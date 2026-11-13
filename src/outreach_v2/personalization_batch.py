"""Durable, authenticated-manifest GPT personalization batch runner."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, exists, func, or_, select

from .db import (candidates, events, invitations, now, personalization_attempts,
    personalization_authorizations, personalization_campaigns)
from .replay_policy import replay_allowed_for_contract
from .screening_campaign import OPEN_ENDED_BUDGET, ids_sha256, EXPECTED_SOURCE_SHA256, EXPECTED_SOURCE_COUNT

BATCH_SIZE = 5
MAX_TOTAL_BUDGET_USD = Decimal('5.00')
MAX_REPLACEMENT_BUDGET_USD = Decimal('2.50')
MAX_FINAL_FILL_BUDGET_USD = Decimal('0.50')
MAX_PER_CANDIDATE_USD = Decimal('0.05')
REPLACEMENT_MANIFEST_SHA256 = '62c74ce1ca5c7b762019ef9261fc70bedd7ff3c0b68ab2fdd85b91254c86ea73'
REPLACEMENT_MANIFEST_EVENT_ID = 'replacement-2026-11-12-v2-62c74ce1ca5c:manifest'
FINAL_FILL_CANDIDATE_IDS = tuple(sorted((
    'osm:041499bd10dadf96f3ee16a1',  # Artel Video Systems Corporation
    'osm:05adb9ce57a77e8a46b861c2',  # Woodstock Playhouse
    'osm:02f48f5e235dc2500fd67f98',  # The Calaveras
    'osm:0549524473b3a493506c4ef1',  # The Blue Beret
    'osm:0120618335516f1f581de9e2',  # Marathon Coffee
    'osm-hotel:node/10178909926',   # Kenoza Hall
    'osm-hotel:node/13460516101',   # Yale Manor B&B and Yurts
    'osm-hotel:node/13538103701',   # Riverside Tower Hotel
    'osm-hotel:node/14270678366',   # The Crafts Inn
    'osm-hotel:node/5480090606',    # Waybury Inn
)))
_active: set[str] = set()
_active_lock = threading.Lock()


def _manifest(row, connection=None):
    ids = json.loads(row['candidate_ids'])
    open_ended = row.get('total_budget_usd') == OPEN_ENDED_BUDGET
    valid_count = (len(ids) > 100 if isinstance(ids, list) and open_ended else
                   isinstance(ids, list) and len(ids) in {10, 50, 100})
    if (not valid_count or len(set(ids)) != len(ids)
            or hashlib_ids(ids) != row['candidate_ids_sha256']):
        raise ValueError('The saved personalization manifest failed its integrity check')
    if open_ended:
        if connection is None or not row.get('campaign_id'):
            raise ValueError('An open-ended manifest must be bound to its immutable historical-screen campaign')
        campaign = connection.execute(select(personalization_campaigns).where(
            personalization_campaigns.c.id == row['campaign_id'],
            personalization_campaigns.c.authorization_id == row['id'],
            personalization_campaigns.c.status.in_(['RUNNING', 'PAUSED', 'COMPLETED', 'ATTENTION_REQUIRED', 'COMPLETED_WITH_BLOCKERS'])
        )).mappings().first()
        if not campaign:
            raise ValueError('The persisted open-ended campaign authorization has no matching campaign')
        source_ids = json.loads(campaign['source_candidate_ids'])
        if (campaign['source_job_id'] != 'full-screening-aIdr0NtN__ISCA'
                or campaign['source_run_version'] != 'full-us-outreach-screening/v2-soft-size'
                or campaign['source_manifest_sha256'] != EXPECTED_SOURCE_SHA256
                or campaign['source_candidate_count'] != EXPECTED_SOURCE_COUNT
                or ids_sha256(source_ids) != EXPECTED_SOURCE_SHA256
                or not set(ids).issubset(set(source_ids))
                or campaign['execution_candidate_ids_sha256'] != row['candidate_ids_sha256']
                or json.loads(campaign['execution_candidate_ids']) != ids):
            raise ValueError('The open-ended campaign is not bound to the exact historical screened cohort')
        return ids
    if len(ids) in {10, 50}:
        if connection is None:
            raise ValueError('A database connection is required to verify the immutable replacement manifest')
        if len(ids) == 50:
            if row['candidate_ids_sha256'] != REPLACEMENT_MANIFEST_SHA256:
                raise ValueError('The 50-candidate authorization is not bound to the verified replacement manifest')
            event_id = REPLACEMENT_MANIFEST_EVENT_ID
            event_name = 'PERSONALIZATION_REPLACEMENT_COHORT_MANIFEST_CREATED'
        else:
            if ids != list(FINAL_FILL_CANDIDATE_IDS):
                raise ValueError('The 10-candidate authorization is not bound to the verified final-fill selection')
            event_id = f'final-fill-{row["candidate_ids_sha256"]}:manifest'
            event_name = 'PERSONALIZATION_FINAL_FILL_COHORT_MANIFEST_CREATED'
        saved = connection.execute(select(events.c.detail).where(
            events.c.id == event_id, events.c.event == event_name)).scalar_one_or_none()
        try:
            detail = json.loads(saved or '{}')
        except (TypeError, ValueError):
            detail = {}
        if len(ids) == 50:
            valid = (detail.get('manifest_sha256') == REPLACEMENT_MANIFEST_SHA256
                and detail.get('candidate_ids') == ids and detail.get('candidate_count') == 50
                and detail.get('kind_breakdown') == {'company': 40, 'hotel': 10})
        else:
            valid = (detail.get('manifest_sha256') == row['candidate_ids_sha256']
                and detail.get('candidate_ids') == ids and detail.get('candidate_count') == 10
                and detail.get('kind_breakdown') == {'company': 5, 'hotel': 5}
                and detail.get('scope') == 'FINAL_FILL_10')
        if not valid:
            raise ValueError('The persisted replacement manifest failed its audit binding check')
    return ids


def manifest_budget(ids):
    return (MAX_FINAL_FILL_BUDGET_USD if len(ids) == 10 else
            MAX_REPLACEMENT_BUDGET_USD if len(ids) == 50 else
            None if len(ids) > 100 else MAX_TOTAL_BUDGET_USD)


def hashlib_ids(ids):
    import hashlib
    return hashlib.sha256(json.dumps(ids, separators=(',', ':')).encode()).hexdigest()


def eligibility(ids, stamp=None):
    stamp = stamp or now()
    # A known, paid provider response that failed local validation can be
    # replayed offline after a validation fix. This narrow exception lets the
    # immutable cohort recover from stale long retry dates without creating a
    # new provider request. Unknown or still-reserved spend remains excluded.
    replayable_response = exists(select(personalization_attempts.c.id).where(
        personalization_attempts.c.candidate_id == candidates.c.id,
        personalization_attempts.c.status.in_(['VALIDATION_FAILED', 'COMPLETED']),
        personalization_attempts.c.actual_cost_usd.is_not(None),
        personalization_attempts.c.actual_cost_usd != '0',
        personalization_attempts.c.provider_response.is_not(None)))
    corrected_validation_outcome = and_(
        or_(candidates.c.personalization_outcome.in_([
                'PROVIDER_JSON_SCHEMA_VALIDATION', 'PROVIDER_EVIDENCE_REFERENCE_VALIDATION',
                'PROVIDER_IDENTITY_VALIDATION', 'PROVIDER_SEMANTIC_CONTRACT_VALIDATION',
                'PROVIDER_STRUCTURED_JSON_PARSE', 'SEMANTIC_QUALITY_REJECTED']),
            and_(candidates.c.personalization_outcome == 'PERMANENT_EVIDENCE',
                 candidates.c.personalization_error.not_in([
                     'No V1 recommendation is supported by this bounded same-site sample; preserve existing data and defer',
                     'HTTP 301', 'HTTP 308', 'HTTPStatusError', 'Website redirect limit exceeded',
                     'Website robots policy could not be checked',
                     'A grounded business description cannot yet be established from the sampled same-site pages',
                     'No visible first-party business claims were available in the permitted page sample']))),
        replayable_response)
    repaired_service_classification = and_(
        candidates.c.personalization_outcome.in_([
            'KNOWN_RESPONSE_REPLAY_REJECTED',
            # One additional offline replay is allowed after the first repair
            # when a URL-based first-party menu classification was deployed.
            'KNOWN_RESPONSE_REPLAY_REJECTED_AFTER_CLASSIFIER_REPAIR',
            # v3 recognizes explicit first-party English catalog and Spanish
            # offer statements on otherwise ordinary About/Home pages.
            'KNOWN_RESPONSE_REPLAY_REJECTED_AFTER_CLASSIFIER_REPAIR_V2',
            # This repair separately checks cited first-party description
            # facts for direct offering language and selects existing
            # BUSINESS_DESCRIPTION claims when their citations overlap exact
            # identity evidence. It does not alter the archived prompt.
            'KNOWN_RESPONSE_REPLAY_REJECTED_AFTER_CLASSIFIER_REPAIR_V3']),
        candidates.c.personalization_error == 'No concise, cited first-party service statement is available',
        replayable_response,
        replay_allowed_for_contract(candidates.c.id, candidates.c.personalization_outcome))
    repaired_cited_description = and_(
        candidates.c.personalization_outcome == 'KNOWN_RESPONSE_REPLAY_REJECTED',
        candidates.c.personalization_error == 'No model-cited, first-party target-business description is available',
        replayable_response)
    # These are narrow, deterministic repairs. The legacy V1 error was raised
    # before website evidence or a provider request existed; it is safe to run
    # under the same manifest authorization. Rejected saved responses are
    # replayed offline only when their known-cost response is present. Identity
    # mismatches are intentionally excluded.
    no_provider_attempt = ~exists(select(personalization_attempts.c.id).where(
        personalization_attempts.c.candidate_id == candidates.c.id))
    legacy_no_geo_recommendation = and_(
        candidates.c.personalization_error ==
            'No V1 recommendation is supported by this bounded same-site sample; preserve existing data and defer',
        candidates.c.personalization_outcome == 'PERMANENT_EVIDENCE',
        no_provider_attempt)
    redirect_or_http_repair = and_(
        candidates.c.personalization_error.in_([
            'HTTP 301', 'HTTP 308', 'HTTPStatusError', 'Website redirect limit exceeded',
            'Website robots policy could not be checked']),
        candidates.c.personalization_outcome.in_(['PERMANENT_EVIDENCE', 'TRANSIENT_HTTP']),
        no_provider_attempt)
    evidence_refresh_no_attempt = ~exists(select(personalization_attempts.c.id).where(
        personalization_attempts.c.candidate_id == candidates.c.id))
    evidence_refresh_not_used = ~exists(select(events.c.id).where(
        events.c.entity_id == candidates.c.id,
        events.c.event == 'PERSONALIZATION_FIRST_PARTY_EVIDENCE_REFRESH_V1'))
    first_party_evidence_refresh = and_(
        candidates.c.id.in_(ids),
        candidates.c.personalization_outcome == 'PERMANENT_EVIDENCE',
        candidates.c.personalization_error.in_([
            'A grounded business description cannot yet be established from the sampled same-site pages',
            'No visible first-party business claims were available in the permitted page sample']),
        evidence_refresh_no_attempt, evidence_refresh_not_used)
    # A known-cost response is replayed only for deterministic repairs that
    # changed the validation contract. A replay that has already failed the
    # current contract is not itself a reason to become eligible again: doing
    # so made the daemon repeatedly select the same five records, report a
    # completed run, spend nothing, and leave every other candidate untouched.
    # Such records require a new, explicit validator repair before eligibility
    # is restored.
    # Only a semantic PASS is complete for this batch. Historical deployments
    # wrote values such as "FAIL" into the analysis-version column; treating
    # every non-NULL value as a completed analysis stranded those legacy rows.
    not_semantically_complete = or_(
        candidates.c.personalization_analysis_version.is_(None),
        candidates.c.personalization_analysis_version != 'semantic-openai-v1',
        and_(candidates.c.personalization_quality_status == 'FAIL',
             candidates.c.personalization_outcome.in_([
                 'SEMANTIC_QUALITY_REJECTED', 'KNOWN_RESPONSE_REPLAY_REJECTED']),
             replayable_response))
    retry_state = or_(candidates.c.personalization_retry_after.is_(None),
                      candidates.c.personalization_retry_after <= stamp,
                      corrected_validation_outcome, repaired_service_classification,
                      repaired_cited_description,
                      legacy_no_geo_recommendation,
                      redirect_or_http_repair, first_party_evidence_refresh)
    return and_(candidates.c.id.in_(ids), candidates.c.kind.in_(['company', 'hotel']),
        candidates.c.fixture.is_(False), candidates.c.suppressed.is_(False),
        or_(candidates.c.personalization_outcome.is_(None),
            candidates.c.personalization_outcome != 'NO_AUTOMATIC_RETRY'),
        candidates.c.website_status == 'VERIFIED', candidates.c.qualification.in_(['ELIGIBLE', 'QUALIFIED']),
        # READY is also the historical legacy personalization state. Only a
        # persisted semantic version is complete for this GPT cohort.
        not_semantically_complete,
        retry_state,
        or_(candidates.c.personalization_lease_until.is_(None), candidates.c.personalization_lease_until <= stamp),
        ~exists(select(invitations.c.id).where(invitations.c.candidate_id == candidates.c.id)),
        ~exists(select(personalization_attempts.c.id).where(
            personalization_attempts.c.candidate_id == candidates.c.id,
            personalization_attempts.c.status.in_(['UNKNOWN_SPEND', 'RESERVED']))))


def progress(db, grant, ids):
    with db.transaction() as c:
        passed = c.scalar(select(func.count()).select_from(candidates).where(
            candidates.c.id.in_(ids), candidates.c.personalization_analysis_version == 'semantic-openai-v1',
            candidates.c.personalization_quality_status == 'PASS')) or 0
        failed = c.scalar(select(func.count()).select_from(candidates).where(
            candidates.c.id.in_(ids), candidates.c.personalization_analysis_version == 'semantic-openai-v1',
            candidates.c.personalization_quality_status == 'FAIL')) or 0
        attempts = c.scalar(select(func.count()).select_from(personalization_attempts).where(
            personalization_attempts.c.candidate_id.in_(ids))) or 0
        ledger = c.execute(select(personalization_attempts.c.status,
            personalization_attempts.c.reserved_usd, personalization_attempts.c.actual_cost_usd).where(
                personalization_attempts.c.candidate_id.in_(ids))).all()
        actual = Decimal('0')
        unresolved = Decimal('0')
        for item in ledger:
            cost = Decimal(str(item.actual_cost_usd or 0))
            actual += cost
            if item.status in {'RESERVED', 'UNKNOWN_SPEND'}:
                unresolved += max(cost, Decimal(str(item.reserved_usd or 0)))
        eligible = c.scalar(select(func.count()).select_from(candidates).where(eligibility(ids))) or 0
        status = grant['status']
        checkpoint = c.execute(select(events.c.created_at, events.c.detail).where(
            events.c.entity_id == grant['id'],
            events.c.event == 'PERSONALIZATION_BATCH_CHECKPOINT').order_by(
                events.c.created_at.desc()).limit(1)).mappings().first()
        no_progress = c.execute(select(events.c.created_at, events.c.detail).where(
            events.c.entity_id == grant['id'],
            events.c.event == 'PERSONALIZATION_BATCH_NO_PROGRESS').order_by(
                events.c.created_at.desc()).limit(1)).mappings().first()
        heartbeat = c.execute(select(events.c.created_at, events.c.detail).where(
            events.c.entity_id == grant['id'], events.c.event.in_([
                'PERSONALIZATION_BATCH_HEARTBEAT', 'PERSONALIZATION_BATCH_CHECKPOINT',
                'PERSONALIZATION_BATCH_WAITING_RETRY', 'PERSONALIZATION_BATCH_NO_PROGRESS'
            ])).order_by(events.c.created_at.desc()).limit(1)).mappings().first()
        retries = c.execute(select(candidates.c.personalization_retry_after).where(
            candidates.c.id.in_(ids), candidates.c.personalization_outcome == 'TRANSIENT_HTTP',
            candidates.c.personalization_attempts < 5,
            candidates.c.personalization_retry_after.is_not(None),
            ~exists(select(personalization_attempts.c.id).where(
                personalization_attempts.c.candidate_id == candidates.c.id,
                personalization_attempts.c.status.in_(['UNKNOWN_SPEND', 'RESERVED']))))).scalars().all()
    runner_active = db.personalization_batch_active() if db.postgres else is_active(grant['id'])
    future_retries = []
    current_time = datetime.now(timezone.utc)
    for retry_at in retries:
        try:
            parsed = datetime.fromisoformat(retry_at)
            parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
            if parsed > current_time:
                future_retries.append(parsed)
        except (TypeError, ValueError):
            continue
    return {'authorization_id': grant['id'], 'status': status, 'candidate_count': len(ids),
        'processed': int(passed + failed), 'eligible_remaining': int(eligible),
        'quality_pass': int(passed), 'quality_fail': int(failed), 'provider_attempts': int(attempts),
        'actual_cost_usd': str(actual), 'unresolved_reserved_usd': str(unresolved),
        'total_budget_cap_usd': str(grant['total_budget_usd']), 'candidate_ids_sha256': grant['candidate_ids_sha256'],
        'runner_active': runner_active,
        'execution_state': ('RUNNER_ACTIVE' if runner_active else 'SCHEDULED_WAIT' if status == 'RUNNING' else status),
        'last_checkpoint_at': checkpoint['created_at'] if checkpoint else None,
        'last_checkpoint': json.loads(checkpoint['detail']) if checkpoint and checkpoint['detail'] else None,
        'heartbeat_at': heartbeat['created_at'] if heartbeat else None,
        'heartbeat': json.loads(heartbeat['detail']) if heartbeat and heartbeat['detail'] else None,
        'recoverable_no_progress': bool(no_progress and no_progress['detail'] and
            (lambda detail: detail.get('blocker') ==
                'The selected candidates produced no attempt or completion; inspect the durable checkpoint before resuming.'
                and (detail.get('last_batch') or {}).get('candidate_ids_sha256') == grant['candidate_ids_sha256']
                and (detail.get('last_batch') or {}).get('attempted') == 0
                and (detail.get('last_batch') or {}).get('completed') == 0)(json.loads(no_progress['detail']))),
        'next_retry_at': min(future_retries).isoformat() if future_retries else None}


def is_active(ident):
    with _active_lock:
        return ident in _active


def _set_status(db, ident, status, event_name, detail=None, *, only_running=False):
    with db.transaction(serialized=True) as c:
        query = select(personalization_authorizations).where(personalization_authorizations.c.id == ident)
        if only_running:
            query = query.where(personalization_authorizations.c.status == 'RUNNING')
        row = c.execute(query.with_for_update()).mappings().first()
        if not row:
            return False
        c.execute(personalization_authorizations.update().where(
            personalization_authorizations.c.id == ident).values(status=status))
        if row.get('campaign_id'):
            c.execute(personalization_campaigns.update().where(
                personalization_campaigns.c.id==row['campaign_id']).values(
                    status=status,completed_at=now() if status in {'COMPLETED','COMPLETED_WITH_BLOCKERS'} else None))
        c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=ident, event=event_name,
            detail=json.dumps(detail or {}, sort_keys=True, separators=(',', ':')), created_at=now()))
        return True


def pause(db, ident):
    return _set_status(db, ident, 'PAUSED', 'PERSONALIZATION_BATCH_PAUSED', {'scope': 'AUTHORIZED_MANIFEST'}, only_running=True)


def launch(db, ident):
    with _active_lock:
        if ident in _active:
            return False
        _active.add(ident)
    threading.Thread(target=_run, args=(db, ident), daemon=True,
        name='personalization-batch-' + ident[-8:]).start()
    return True


def _sleep_while_running(db, ident, seconds):
    until = time.monotonic() + max(0, seconds)
    while time.monotonic() < until:
        with db.transaction() as c:
            status = c.scalar(select(personalization_authorizations.c.status).where(
                personalization_authorizations.c.id == ident))
        if status != 'RUNNING':
            return False
        time.sleep(min(2, max(0.05, until - time.monotonic())))
    return True


def _retry_wait(db, ids):
    """Return seconds until the next ordinary transient retry, if any."""
    from datetime import datetime
    with db.transaction() as c:
        rows = c.execute(select(candidates.c.personalization_retry_after).where(
            candidates.c.id.in_(ids), candidates.c.personalization_status == 'INCOMPLETE',
            candidates.c.personalization_outcome == 'TRANSIENT_HTTP',
            candidates.c.personalization_attempts < 5,
            candidates.c.personalization_retry_after.is_not(None),
            ~exists(select(personalization_attempts.c.id).where(
                personalization_attempts.c.candidate_id == candidates.c.id,
                personalization_attempts.c.status.in_(['UNKNOWN_SPEND', 'RESERVED']))))).scalars().all()
    future = []
    current = datetime.now(timezone.utc)
    for value in rows:
        try:
            retry_at = datetime.fromisoformat(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delta = (retry_at - current).total_seconds()
            if delta > 0:
                future.append(delta)
        except (TypeError, ValueError):
            continue
    return min(future) if future else None


def _run(db, ident):
    try:
        with db.personalization_batch_lock() as acquired:
            if acquired:
                _run_locked(db, ident)
    finally:
        with _active_lock:
            _active.discard(ident)


def _run_locked(db, ident):
    try:
        with db.transaction() as c:
            grant = c.execute(select(personalization_authorizations).where(
                personalization_authorizations.c.id == ident)).mappings().first()
        if not grant:
            return
        with db.transaction() as c:
            ids = _manifest(grant, c)
        cohort_cap = manifest_budget(ids)
        with db.transaction() as c:
            c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=ident,
                event='PERSONALIZATION_BATCH_HEARTBEAT',
                detail=json.dumps({'execution_state':'RUNNER_ACTIVE',
                    'candidate_ids_sha256':grant['candidate_ids_sha256']},
                    sort_keys=True, separators=(',', ':')), created_at=now()))
        while True:
            with db.transaction() as c:
                current = c.execute(select(personalization_authorizations).where(
                    personalization_authorizations.c.id == ident)).mappings().first()
                if not current or current['status'] != 'RUNNING':
                    return
                next_ids = c.execute(select(candidates.c.id).where(eligibility(ids)).order_by(
                    candidates.c.personalization_at.asc().nulls_first(), candidates.c.id).limit(BATCH_SIZE)).scalars().all()
            if not next_ids:
                delay = _retry_wait(db, ids)
                if delay is not None:
                    with db.transaction() as c:
                        c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=ident,
                            event='PERSONALIZATION_BATCH_WAITING_RETRY',
                            detail=json.dumps({'retry_in_seconds': int(delay)}, separators=(',', ':')),
                            created_at=now()))
                        c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=ident,
                            event='PERSONALIZATION_BATCH_HEARTBEAT',
                            detail=json.dumps({'execution_state':'SCHEDULED_WAIT',
                                'next_retry_in_seconds':int(delay)}, separators=(',', ':')),
                            created_at=now()))
                    return
                current_progress = progress(db, current, ids)
                if cohort_cap is None:
                    from .screening_campaign import terminal_summary
                    with db.transaction() as c:
                        campaign=c.execute(select(personalization_campaigns).where(
                            personalization_campaigns.c.authorization_id==ident)).mappings().first()
                    terminal=terminal_summary(db, json.loads(campaign['source_candidate_ids']), ids)
                    current_progress.update(terminal)
                    final='COMPLETED_WITH_BLOCKERS' if terminal['pending_count']==0 else 'ATTENTION_REQUIRED'
                else:
                    final = ('ATTENTION_REQUIRED'
                        if (Decimal(current_progress['unresolved_reserved_usd']) > 0
                            or current_progress['processed'] < current_progress['candidate_count'])
                        else 'COMPLETED')
                _set_status(db, ident, final, 'PERSONALIZATION_BATCH_FINISHED', current_progress, only_running=True)
                return

            from .personalization import backfill
            batch_options = {'approved_cumulative_budget_usd':str(MAX_TOTAL_BUDGET_USD),
                'approved_cohort_budget_usd':str(cohort_cap),
                'approved_cohort_candidate_ids':ids if len(ids) in {10, 50} else None}
            if cohort_cap is None:
                batch_options = {'open_ended_campaign_authorization_id':ident,
                    'open_ended_campaign_manifest_sha256':grant['candidate_ids_sha256'],
                    'open_ended_campaign_candidate_ids':ids,
                    'approved_per_candidate_budget_usd':str(MAX_PER_CANDIDATE_USD)}
            result = backfill(db, limit=len(next_ids), concurrency=1, time_budget_seconds=300,
                authorized_candidate_ids=next_ids, retry_candidate_ids=next_ids, **batch_options)
            detail = {'candidate_count': len(next_ids), 'candidate_ids_sha256': grant['candidate_ids_sha256'],
                'candidate_ids': list(next_ids),
                'run_id': result.get('run_id'), 'attempted': result.get('attempted', 0),
                'completed': result.get('completed', 0), 'incomplete': result.get('incomplete', 0),
                'status': result.get('status'), 'budget_stopped': result.get('budget_stopped', False),
                'actual_cost_usd': result.get('actual_cost_usd', '0'),
                'unresolved_reserved_usd': result.get('unresolved_reserved_usd', '0')}
            with db.transaction() as c:
                c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=ident,
                    event='PERSONALIZATION_BATCH_CHECKPOINT',
                    detail=json.dumps(detail, sort_keys=True, separators=(',', ':')), created_at=now()))
                if current.get('campaign_id'):
                    c.execute(personalization_campaigns.update().where(
                        personalization_campaigns.c.id==current['campaign_id']).values(last_checkpoint_at=now()))
            if result.get('status') in {'BLOCKED_CONFIGURATION', 'DISABLED'} or (
                    result.get('budget_stopped') and result.get('attempted', 0) == 0):
                current_progress = progress(db, grant, ids)
                current_progress['last_batch'] = detail
                _set_status(db, ident, 'ATTENTION_REQUIRED', 'PERSONALIZATION_BATCH_ATTENTION_REQUIRED',
                    current_progress, only_running=True)
                return
            if (result.get('attempted', 0) > 0 and result.get('completed', 0) == 0
                    and result.get('replayed', 0) >= result.get('attempted', 0)
                    and Decimal(str(result.get('actual_cost_usd', '0'))) == 0):
                # Re-evaluate the durable selectors. Other candidates can
                # proceed in this bounded tick; if none remain, the next loop
                # iteration records an accurate terminal/attention state.
                with db.transaction() as c:
                    c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=ident,
                        event='PERSONALIZATION_BATCH_HEARTBEAT',
                        detail=json.dumps({'execution_state':'SCHEDULED_WAIT',
                            'reason':'OFFLINE_REPLAY_CHUNK_COMPLETE',
                            'candidate_ids':list(next_ids)},sort_keys=True,separators=(',',':')),
                        created_at=now()))
                continue
            if result.get('attempted', 0) == 0 and result.get('completed', 0) == 0:
                if result.get('skipped_existing_attempt', 0) > 0:
                    with db.transaction() as c:
                        c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=ident,
                            event='PERSONALIZATION_BATCH_HEARTBEAT',
                            detail=json.dumps({'execution_state':'SCHEDULED_WAIT',
                                'reason':'SAFE_REPLAY_NOT_PROVEN_BY_ARCHIVED_PROMPT_HASH',
                                'candidate_ids':list(next_ids)},sort_keys=True,separators=(',',':')),
                            created_at=now()))
                    return
                current_progress = progress(db, grant, ids)
                current_progress['last_batch'] = detail
                current_progress['blocker'] = 'The selected candidates produced no attempt or completion; inspect the durable checkpoint before resuming.'
                _set_status(db, ident, 'ATTENTION_REQUIRED',
                    'PERSONALIZATION_BATCH_NO_PROGRESS', current_progress, only_running=True)
                return
            # Work one bounded chunk per invocation. A scheduled authenticated
            # tick reacquires the persisted RUNNING grant after this thread
            # exits, and after any Render restart.
            return
    except Exception as exc:
        _set_status(db, ident, 'ATTENTION_REQUIRED', 'PERSONALIZATION_BATCH_RUNNER_ERROR',
            {'error_type': type(exc).__name__}, only_running=True)
    finally:
        with _active_lock:
            _active.discard(ident)
