"""Durable, authenticated-manifest GPT personalization batch runner."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, exists, func, or_, select

from .db import candidates, events, invitations, now, personalization_attempts, personalization_authorizations

BATCH_SIZE = 5
MAX_TOTAL_BUDGET_USD = Decimal('5.00')
MAX_PER_CANDIDATE_USD = Decimal('0.05')
_active: set[str] = set()
_active_lock = threading.Lock()


def _manifest(row):
    ids = json.loads(row['candidate_ids'])
    if (not isinstance(ids, list) or len(ids) != 100 or len(set(ids)) != 100
            or hashlib_ids(ids) != row['candidate_ids_sha256']):
        raise ValueError('The saved 100-candidate manifest failed its integrity check')
    return ids


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
                     'Website robots policy could not be checked']))),
        replayable_response)
    repaired_service_classification = and_(
        candidates.c.personalization_outcome.in_([
            'KNOWN_RESPONSE_REPLAY_REJECTED',
            # One additional offline replay is allowed after the first repair
            # when a new URL-based first-party menu classification is deployed.
            'KNOWN_RESPONSE_REPLAY_REJECTED_AFTER_CLASSIFIER_REPAIR']),
        candidates.c.personalization_error == 'No concise, cited first-party service statement is available',
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
                      legacy_no_geo_recommendation,
                      redirect_or_http_repair)
    return and_(candidates.c.id.in_(ids), candidates.c.kind.in_(['company', 'hotel']),
        candidates.c.fixture.is_(False), candidates.c.suppressed.is_(False),
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
        c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=ident, event=event_name,
            detail=json.dumps(detail or {}, sort_keys=True, separators=(',', ':')), created_at=now()))
        return True


def pause(db, ident):
    return _set_status(db, ident, 'PAUSED', 'PERSONALIZATION_BATCH_PAUSED', {'scope': 'COHORT_100'}, only_running=True)


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
        ids = _manifest(grant)
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
                final = 'ATTENTION_REQUIRED' if Decimal(current_progress['unresolved_reserved_usd']) > 0 else 'COMPLETED'
                _set_status(db, ident, final, 'PERSONALIZATION_BATCH_FINISHED', current_progress, only_running=True)
                return

            from .personalization import backfill
            result = backfill(db, limit=len(next_ids), concurrency=1, time_budget_seconds=300,
                authorized_candidate_ids=next_ids, retry_candidate_ids=next_ids,
                approved_cumulative_budget_usd=str(MAX_TOTAL_BUDGET_USD))
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
                # Rejected known-cost responses are made ineligible by the
                # repair-specific outcome. Let the scheduler advance to other
                # IDs instead of resetting or endlessly replaying this chunk.
                with db.transaction() as c:
                    c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=ident,
                        event='PERSONALIZATION_BATCH_HEARTBEAT',
                        detail=json.dumps({'execution_state':'SCHEDULED_WAIT',
                            'reason':'OFFLINE_REPLAY_CHUNK_COMPLETE',
                            'candidate_ids':list(next_ids)},sort_keys=True,separators=(',',':')),
                        created_at=now()))
                return
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
