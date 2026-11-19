"""Durable, chunked invitation generation. This module prepares review packages only."""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, or_, func, bindparam

from . import service
from .db import (batches, candidates, contacts, email_templates, events, invitation_generation_batches,
                 invitation_generation_items, invitations, questionnaires, now, worker_runs)
from .migrate import dump, parse, sha
from .package import build_package
from .template import BULK_SUBJECT, host, render
from outreach_agent.google_forms_integration import prefilled_form_url

MAX_ITEMS = 1000
DEFAULT_CHUNK_SIZE = 25
MAX_BATCH_ATTEMPTS = 8


def worker_limits():
    """Bound durable generation work per scheduled request; never accept unbounded config."""
    import os
    try:
        chunks=int(os.environ.get('OUTREACH_INVITATION_GENERATION_CHUNKS_PER_TICK','4'))
        size=int(os.environ.get('OUTREACH_INVITATION_GENERATION_CHUNK_SIZE',str(DEFAULT_CHUNK_SIZE)))
    except (TypeError,ValueError) as exc:
        raise ValueError('Invitation generation worker limits must be integers') from exc
    if not 1<=chunks<=4 or not 1<=size<=100:
        raise ValueError('Invitation generation worker limits exceed safe bounds')
    return chunks,size


def _fingerprint(row):
    fields = ('id', 'kind', 'name', 'website', 'domain', 'industry', 'location', 'suppressed',
              'website_status', 'qualification', 'fixture', 'personalization',
              'personalization_status', 'personalization_quality_status', 'personalization_quality_errors',
              'personalization_version', 'personalization_analysis_version', 'personalization_fingerprint')
    return sha(dump({key: row.get(key) for key in fields}))


def enqueue(db, selection, request_key):
    """Validate the reviewed preview and durably snapshot at most 1000 candidates."""
    if not isinstance(request_key, str) or not 16 <= len(request_key) <= 128:
        raise ValueError('A stable request key is required')
    if not isinstance(selection, dict) or set(selection) - {'ids', 'filters', 'all_matching', 'recipient_choices', 'preview_sha256'}:
        raise ValueError('Invalid selection')
    if not selection.get('preview_sha256'):
        raise ValueError('Review the current recipient preview before generating a batch')
    selection_core = {k: v for k, v in selection.items() if k != 'preview_sha256'}
    selection_hash = sha(dump(selection_core))
    with db.transaction(serialized=True) as c:
        prior = c.execute(select(batches).where(batches.c.request_key == request_key)).mappings().first()
        if prior:
            if prior['selection_hash'] != selection_hash:
                raise ValueError('Request key belongs to a different selection')
            state = c.execute(select(invitation_generation_batches).where(
                invitation_generation_batches.c.batch_id == prior['id'])).mappings().first()
            return {'batch_id': prior['id'], 'generation_status': state['status'] if state else 'COMPLETED',
                    'total_items': state['total_items'] if state else 0,
                    'completed_items': state['completed_items'] if state else 0,
                    'invitation_count': c.scalar(select(func.count()).select_from(invitations).where(
                        invitations.c.batch_id == prior['id'])),
                    'idempotent_replay': True}

        context = {}
        review = service._preview_in_transaction(c, selection, context=context)
        if review['preview_sha256'] != selection['preview_sha256']:
            raise ValueError('Recipient eligibility changed after review; refresh the preview before generating')
        if review['selected_total'] > MAX_ITEMS:
            raise ValueError(f'One durable invitation batch is limited to {MAX_ITEMS} selected candidates')
        eligible = context.get('eligible', [])
        if not eligible:
            raise ValueError('No eligible invitations: ' + dump(review['excluded']) + '; no invitations were created')

        questionnaire = context.get('questionnaire')
        template = context.get('invitation_template')
        batch_id = 'batch-' + uuid.uuid4().hex
        stamp = now()
        snapshot = {
            'selection_hash': selection_hash,
            'questionnaire': dict(questionnaire) if questionnaire else None,
            'template': dict(template) if template else None,
            'bulk_subject': BULK_SUBJECT,
            'selection': selection_core,
        }
        c.execute(batches.insert().values(id=batch_id, request_key=request_key, selection_hash=selection_hash,
            created_at=stamp, bulk_subject=BULK_SUBJECT, pilot_review_status='GENERATING',
            excluded=dump(review['excluded'])))
        c.execute(invitation_generation_batches.insert().values(batch_id=batch_id, status='QUEUED',
            selection_snapshot=dump(snapshot), excluded_snapshot=dump(review['excluded']),
            total_items=len(eligible), completed_items=0, attempts=0, created_at=stamp, updated_at=stamp))
        items = []
        choices = selection_core.get('recipient_choices') or {}
        for ordinal, (row, recipient) in enumerate(eligible):
            frozen = dict(row)
            items.append({'id': f'{batch_id}-item-{ordinal:04d}', 'batch_id': batch_id, 'ordinal': ordinal,
                'candidate_id': row['id'], 'candidate_snapshot': dump({'row': frozen,
                    'fingerprint': _fingerprint(frozen), 'recipient_choice': choices.get(row['id'])}),
                'recipient': recipient, 'status': 'PENDING', 'created_at': stamp})
        c.execute(invitation_generation_items.insert(), items)
        c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=batch_id,
            event='INVITATION_BATCH_QUEUED', detail=dump({'selected_total': review['selected_total'],
                'eligible_total': len(items), 'excluded_total': len(review['excluded']),
                'preview_sha256': review['preview_sha256'], 'chunk_size': DEFAULT_CHUNK_SIZE}), created_at=stamp))
        return {'batch_id': batch_id, 'generation_status': 'QUEUED', 'total_items': len(items),
                'completed_items': 0, 'invitation_count': None, 'idempotent_replay': False}


def _invitation_row(row, recipient, template, questionnaire):
    p = parse(row['personalization'])
    fields = parse(questionnaire['field_map']) if questionnaire else {}
    form_url = questionnaire['form_url'] if questionnaire else None
    entry_id = fields.get('invitation_reference_entry_id') if questionnaire else None
    invitation_id = 'inv-' + uuid.uuid4().hex
    reference = 'GEO-' + secrets.token_hex(16).upper()
    url = prefilled_form_url(reference, form_url=form_url, entry_id=entry_id)
    subject, body = render(row['name'], row['website'], p, url, reference, record=row, template_config=template)
    authority = {'method': 'INVITATION_EMAIL_AND_FIRST_PARTY_DOMAIN', 'website': row['website']}
    if row['fixture']:
        binding = service.OWNER_BINDINGS.get(host(row['website']))
        if not binding or binding['recipient'] != recipient:
            raise ValueError('Owner recipient binding mismatch')
        authority = {'method': 'OWNER_BINDING', 'website': row['website'], **binding}
    env = __import__('os').environ
    return dict(id=invitation_id, candidate_id=row['id'], batch_id=None, reference=reference,
        recipient=recipient, website=row['website'], company_name=row['name'], subject=subject, body=body,
        form_url=url, template_version=(f"invitation-template-v{template['version']}" if template else service.VERSION),
        questionnaire_id=questionnaire['id'] if questionnaire else None,
        researcher_name=(template['researcher_name'] if template else env.get('OUTREACH_RESEARCHER_NAME')),
        university_affiliation=(template['university_affiliation'] if template else env.get('OUTREACH_UNIVERSITY_AFFILIATION')),
        reply_to=(template['reply_to'] if template else env.get('OUTREACH_REPLY_TO')),
        sender_name=(template.get('sender_name') if template else (env.get('OUTREACH_INVITATION_SENDER_NAME') or env.get('OUTREACH_RESEARCHER_NAME'))),
        sender_email=(template['sender'] if template else (env.get('OUTREACH_INVITATION_SENDER') or env.get('OUTREACH_REPLY_TO'))),
        personalization=dump(p), generated_at=now(), exported_at=None, send_status='UNVERIFIED',
        consent_status='NOT_RECEIVED', authority=dump(authority))


def process_one(db, *, chunk_size=DEFAULT_CHUNK_SIZE, batch_id=None):
    if type(chunk_size) is not int or not 1 <= chunk_size <= 100:
        raise ValueError('Invitation generation chunk size must be between 1 and 100')
    token = uuid.uuid4().hex
    stamp = now()
    finalization=None
    with db.transaction(serialized=True) as c:
        eligibility = [invitation_generation_batches.c.status.in_(['QUEUED', 'PROCESSING', 'RETRY_PENDING','FINALIZING']),
            invitation_generation_batches.c.recovery_attempts < MAX_BATCH_ATTEMPTS,
            or_(invitation_generation_batches.c.next_attempt_at.is_(None), invitation_generation_batches.c.next_attempt_at <= stamp),
            or_(invitation_generation_batches.c.lease_until.is_(None), invitation_generation_batches.c.lease_until < stamp)]
        if batch_id: eligibility.append(invitation_generation_batches.c.batch_id == batch_id)
        gen = c.execute(select(invitation_generation_batches).where(*eligibility)
            .order_by(invitation_generation_batches.c.created_at).limit(1)).mappings().first()
        if not gen: return None
        c.execute(invitation_generation_batches.update().where(
            invitation_generation_batches.c.batch_id == gen['batch_id']).values(
                status='FINALIZING' if gen['status']=='FINALIZING' else 'PROCESSING', lease_token=token,
                lease_until=(datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
                attempts=gen['attempts'] + 1, updated_at=stamp, error=None))
        pending = c.execute(select(invitation_generation_items).where(
            invitation_generation_items.c.batch_id == gen['batch_id'],
            invitation_generation_items.c.status == 'PENDING').order_by(
                invitation_generation_items.c.ordinal).limit(chunk_size)).mappings().all()
        snapshot = parse(gen['selection_snapshot'])
        config = snapshot['template']
        questionnaire = snapshot['questionnaire']
        rows_by_id = {r['id']: dict(r) for r in c.execute(select(candidates).where(
            candidates.c.id.in_([i['candidate_id'] for i in pending]))).mappings()} if pending else {}
        lookup = service._generation_lookup(c, list(rows_by_id.values())) if rows_by_id else None
        generated = []
        item_updates = []
        for item in pending:
            frozen = parse(item['candidate_snapshot'])
            row = rows_by_id.get(item['candidate_id'])
            reason = None
            if row is None or _fingerprint(row) != frozen['fingerprint']:
                reason = 'Candidate changed after the reviewed selection snapshot'
            else:
                recipient, reasons = service.eligible_recipient(c, row, frozen.get('recipient_choice'), lookup=lookup)
                if recipient != item['recipient'] or reasons:
                    reason = '; '.join(reasons) or 'Authorized recipient changed after review'
                elif item['recipient'].strip().casefold() in lookup['existing_invited_recipients']:
                    reason = 'Recipient already has an invitation; duplicate generation is blocked'
            if reason:
                item_updates.append({'_item_id':item['id'],'_status':'SKIPPED',
                    '_invitation_id':None,'_error':reason})
                continue
            try:
                invitation = _invitation_row(row, item['recipient'], config, questionnaire)
                invitation['batch_id'] = gen['batch_id']
            except (ValueError, TypeError) as exc:
                item_updates.append({'_item_id':item['id'],'_status':'FAILED',
                    '_invitation_id':None,'_error':str(exc)[:500]})
                continue
            item_updates.append({'_item_id':item['id'],'_status':'GENERATED',
                '_invitation_id':invitation['id'],'_error':None})
            generated.append(invitation)
            lookup['candidate_ids_with_invitations'].add(row['id'])
            lookup['existing_invited_recipients'].add(item['recipient'].strip().casefold())
        # Keep all chunk writes in the existing transaction, but send rows to
        # the database as executemany batches instead of one round trip per
        # invitation and per item. A database error still rolls back the whole
        # chunk, leaving its durable snapshot safe to replay.
        if generated:
            c.execute(invitations.insert(), generated)
        if item_updates:
            item_update = invitation_generation_items.update().where(
                invitation_generation_items.c.id == bindparam('_item_id')).values(
                    status=bindparam('_status'), invitation_id=bindparam('_invitation_id'),
                    error=bindparam('_error'))
            c.execute(item_update, item_updates)
        completed = c.scalar(select(invitation_generation_items.c.id).where(
            invitation_generation_items.c.batch_id == gen['batch_id'],
            invitation_generation_items.c.status == 'PENDING').limit(1)) is None
        skipped_failed = c.scalar(select(invitation_generation_items.c.id).where(
            invitation_generation_items.c.batch_id == gen['batch_id'],
            invitation_generation_items.c.status.in_(['SKIPPED', 'FAILED'])).limit(1)) is not None
        c.execute(invitation_generation_batches.update().where(
            invitation_generation_batches.c.batch_id == gen['batch_id'],
            invitation_generation_batches.c.lease_token == token).values(
                status='FINALIZING' if completed else 'PROCESSING', completed_items=gen['completed_items'] + len(pending),
                lease_until=None, lease_token=None, updated_at=now()))
        c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=gen['batch_id'],
            event='INVITATION_BATCH_CHUNK_COMPLETED', detail=dump({'processed': len(pending),
                'generated': len(generated), 'remaining': max(0, gen['total_items'] - gen['completed_items'] - len(pending))}),
            created_at=now()))
        if not completed:
            return {'batch_id': gen['batch_id'], 'status': 'PROCESSING',
                    'completed_items': gen['completed_items'] + len(pending), 'total_items': gen['total_items']}
        invitations_for_package = [dict(r) for r in c.execute(select(invitations).where(
            invitations.c.batch_id == gen['batch_id']).order_by(invitations.c.company_name)).mappings()]
        excluded = parse(gen['excluded_snapshot'])
        for item in c.execute(select(invitation_generation_items).where(
                invitation_generation_items.c.batch_id == gen['batch_id'],
                invitation_generation_items.c.status.in_(['SKIPPED', 'FAILED'])).order_by(
                    invitation_generation_items.c.ordinal)).mappings():
            excluded.append({'candidate_id': item['candidate_id'], 'reasons': [item['error'] or item['status']]})
        if not invitations_for_package:
            c.execute(invitation_generation_batches.update().where(
                invitation_generation_batches.c.batch_id == gen['batch_id']).values(
                    status='BLOCKED', error='No eligible invitations survived final validation', lease_token=None,
                    lease_until=None, updated_at=now()))
            c.execute(batches.update().where(batches.c.id == gen['batch_id']).values(
                pilot_review_status='BLOCKED_INCOMPLETE', excluded=dump(excluded)))
            return {'batch_id': gen['batch_id'], 'status': 'BLOCKED', 'completed_items': gen['total_items'],
                    'total_items': gen['total_items']}
        finalization=(gen['batch_id'],invitations_for_package,excluded,snapshot['bulk_subject'],
                      skipped_failed or bool(excluded),gen['total_items'])
    batch_id,package_rows,excluded,bulk_subject,has_exclusions,total_items=finalization
    package=build_package(package_rows,excluded,bulk_subject)
    digest=hashlib.sha256(package).hexdigest()
    final_status='COMPLETED_WITH_EXCLUSIONS' if has_exclusions else 'COMPLETED'
    with db.transaction(serialized=True) as c:
        current=c.execute(select(invitation_generation_batches.c.status).where(
            invitation_generation_batches.c.batch_id==batch_id)).scalar_one()
        if current!='FINALIZING':
            return {'batch_id':batch_id,'status':current,'completed_items':total_items,'total_items':total_items}
        c.execute(batches.update().where(batches.c.id==batch_id).values(
            package=package,excluded=dump(excluded),exported_at=now(),pilot_review_status='PENDING_REVIEW'))
        c.execute(invitation_generation_batches.update().where(
            invitation_generation_batches.c.batch_id==batch_id).values(
                status=final_status,lease_token=None,lease_until=None,updated_at=now(),error=None))
        c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=batch_id,event='EXPORTED',
            detail=dump({'count':len(package_rows),'excluded':len(excluded),'package_sha256':digest}),created_at=now()))
    return {'batch_id':batch_id,'status':final_status,'completed_items':total_items,
            'total_items':total_items,'invitation_count':len(package_rows),'package_sha256':digest}


def process_pending(db, *, max_batches=1, chunk_size=DEFAULT_CHUNK_SIZE):
    if type(max_batches) is not int or not 0 <= max_batches <= 4:
        raise ValueError('At most four invitation generation chunks may be processed per scheduler tick')
    results=[]
    for _ in range(max_batches):
        try:
            result=process_one(db,chunk_size=chunk_size)
        except Exception as exc:
            # A failed chunk rolls back all item and invitation writes. Record a
            # bounded diagnostic and leave the same immutable snapshot resumable.
            try:
                with db.transaction(serialized=True) as c:
                    gen=c.execute(select(invitation_generation_batches).where(
                        invitation_generation_batches.c.status.in_(['QUEUED','PROCESSING','RETRY_PENDING','FINALIZING']))
                        .order_by(invitation_generation_batches.c.created_at).limit(1)).mappings().first()
                    if gen:
                        diagnostic=(str(exc)[:300] if isinstance(exc,(ValueError,TypeError))
                                     else type(exc).__name__+'; chunk rolled back and remains recoverable')
                        attempts=gen['recovery_attempts']+1
                        blocked=attempts>=MAX_BATCH_ATTEMPTS
                        retry=None if blocked else (datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
                        c.execute(invitation_generation_batches.update().where(
                            invitation_generation_batches.c.batch_id==gen['batch_id']).values(
                                status='BLOCKED' if blocked else 'RETRY_PENDING',error=diagnostic,
                                next_attempt_at=retry,recovery_attempts=attempts,lease_token=None,lease_until=None,updated_at=now()))
                        c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=gen['batch_id'],
                            event='INVITATION_BATCH_CHUNK_BLOCKED' if blocked else 'INVITATION_BATCH_CHUNK_RETRY_PENDING',
                            detail=dump({'error':diagnostic,'attempts':attempts}),created_at=now()))
                        results.append({'batch_id':gen['batch_id'],'status':'BLOCKED' if blocked else 'RETRY_PENDING',
                            'completed_items':gen['completed_items'],'total_items':gen['total_items'],'error':diagnostic})
            except Exception:
                pass
            break
        if result is None:break
        results.append(result)
        if result['status'] not in {'PROCESSING'}:break
    return results


def progress(db, batch_id):
    with db.transaction() as c:
        gen=c.execute(select(invitation_generation_batches).where(
            invitation_generation_batches.c.batch_id==batch_id)).mappings().first()
        batch=c.execute(select(batches).where(batches.c.id==batch_id)).mappings().first()
        if not batch:raise LookupError('Invitation batch not found')
        if not gen:
            return {'batch_id':batch_id,'status':'COMPLETED','completed_items':0,'total_items':0,
                    'invitation_count':c.scalar(select(func.count()).select_from(invitations).where(invitations.c.batch_id==batch_id))}
        count=c.scalar(select(func.count()).select_from(invitations).where(invitations.c.batch_id==batch_id))
        return {'batch_id':batch_id,'status':gen['status'],'completed_items':gen['completed_items'],
                'total_items':gen['total_items'],'invitation_count':count,'error':gen['error'],
                'review_status':batch['pilot_review_status'],'download_available':batch['pilot_review_status']=='APPROVED'}


def resume(db,batch_id):
    """Explicitly resume the same durable snapshot after bounded automatic retries."""
    with db.transaction(serialized=True) as c:
        gen=c.execute(select(invitation_generation_batches).where(
            invitation_generation_batches.c.batch_id==batch_id)).mappings().first()
        if not gen:raise LookupError('Invitation generation batch not found')
        if gen['status']!='BLOCKED':raise ValueError('Only a blocked generation batch can be resumed')
        if gen['lease_token'] or (gen['lease_until'] and gen['lease_until']>now()):
            raise ValueError('Invitation generation batch still has an active worker lease')
        c.execute(invitation_generation_batches.update().where(
            invitation_generation_batches.c.batch_id==batch_id).values(
                status='RETRY_PENDING',recovery_attempts=0,next_attempt_at=now(),lease_token=None,
                lease_until=None,error=None,updated_at=now()))
        c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=batch_id,
            event='INVITATION_BATCH_RECOVERY_AUTHORIZED',detail=dump({'prior_recovery_attempts':gen['recovery_attempts'],
                'cumulative_worker_attempts':gen['attempts'],'completed_items':gen['completed_items'],
                'snapshot_sha256':sha(gen['selection_snapshot'])}),created_at=now()))
        return {'batch_id':batch_id,'status':'RETRY_PENDING','completed_items':gen['completed_items'],
                'total_items':gen['total_items'],'preserved_snapshot':True}


def tick(db):
    """One auditable durable worker invocation; handles invitation drafts only."""
    run_id=uuid.uuid4().hex
    with db.transaction() as c:
        c.execute(worker_runs.insert().values(id=run_id,started_at=now(),status='RUNNING',detail='{}'))
    error=None
    try:
        max_batches,chunk_size=worker_limits()
        results=process_pending(db,max_batches=max_batches,chunk_size=chunk_size)
    except Exception:
        results=[]
        error='Invitation generation worker failed; durable batch state remains available for recovery'
    needs_attention=bool(error) or any(item.get('status') in {'RETRY_PENDING','BLOCKED'} for item in results)
    result={'worker_run_id':run_id,'status':'ATTENTION_REQUIRED' if needs_attention else 'COMPLETED',
            'processed_chunks':len(results),'invitation_generation':results,
            'error':error,'external_provider_calls':0,'emails_sent':0}
    with db.transaction() as c:
        c.execute(worker_runs.update().where(worker_runs.c.id==run_id).values(
            finished_at=now(),status=result['status'],detail=dump(result)))
    return result
