"""Immutable binding to the original full-population deterministic screen."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select, text

from .db import candidates, invitations, personalization_attempts

SOURCE_JOB_ID = 'full-screening-aIdr0NtN__ISCA'
SOURCE_RUN_VERSION = 'full-us-outreach-screening/v2-soft-size'
SOURCE_SCREEN_STATUS = 'ELIGIBLE_FOR_AI_SCREENING'
EXPECTED_SOURCE_COUNT = 5362
EXPECTED_COMPANY_COUNT = 5077
EXPECTED_HOTEL_COUNT = 285
EXPECTED_SOURCE_SHA256 = 'a429c6055896ef7ac183af42a6c0ec9df5f3d98389618397ebc8484d45c11595'
OPEN_ENDED_BUDGET = 'OPEN_ENDED'

SCREENING_CRITERIA = {
    'source': 'public.full_screening_queue and public.full_screening_jobs',
    'job_id': SOURCE_JOB_ID,
    'run_version': SOURCE_RUN_VERSION,
    'membership': 'eligibility_status=ELIGIBLE_FOR_AI_SCREENING',
    'deterministic_gate': [
        'not owner-controlled test', 'verified US geography', 'verified identity',
        'eligible qualification', 'official website present', 'valid public business email',
        'not declined or withdrawn', 'not excluded or unsubscribed',
        'employee/revenue size is a soft preference only',
    ],
    'provider_status': 'PAUSED_AT_AI_BOUNDARY; no paid AI screening completed',
}


def ids_sha256(ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(ids, separators=(',', ':')).encode('utf-8')).hexdigest()


def load_original_screen(connection) -> dict:
    """Read the exact frozen pass set and fail closed if any source fact changed."""
    job = connection.execute(text(
        'SELECT job_id,run_version,status FROM public.full_screening_jobs WHERE job_id=:job_id'
    ), {'job_id': SOURCE_JOB_ID}).mappings().first()
    if not job or job['run_version'] != SOURCE_RUN_VERSION or job['status'] != 'PAUSED_AT_AI_BOUNDARY':
        raise ValueError('The original deterministic screening job is missing or its saved outcome changed')
    rows = connection.execute(text(
        'SELECT profile_id,cohort,eligibility_status,blocking_reasons_json,status '
        'FROM public.full_screening_queue WHERE job_id=:job_id '
        'AND eligibility_status=:screen_status ORDER BY profile_id'
    ), {'job_id': SOURCE_JOB_ID, 'screen_status': SOURCE_SCREEN_STATUS}).mappings().all()
    ids = [str(row['profile_id']) for row in rows]
    if (len(ids) != EXPECTED_SOURCE_COUNT or len(set(ids)) != EXPECTED_SOURCE_COUNT
            or ids_sha256(ids) != EXPECTED_SOURCE_SHA256):
        raise ValueError('The historical initial-screen membership differs from its verified immutable count/hash')
    breakdown = {kind: sum(1 for row in rows if row['cohort'] == kind)
                 for kind in ('business_company', 'hotel_property')}
    if breakdown != {'business_company': EXPECTED_COMPANY_COUNT, 'hotel_property': EXPECTED_HOTEL_COUNT}:
        raise ValueError('The historical screening company/hotel breakdown differs from the verified source')
    nonempty_blockers = [row['profile_id'] for row in rows if json.loads(row['blocking_reasons_json'] or '[]')]
    if nonempty_blockers:
        raise ValueError('A candidate marked eligible by the original screen has saved blocking reasons')
    live = set(connection.execute(select(candidates.c.id).where(candidates.c.id.in_(ids))).scalars())
    if live != set(ids):
        raise ValueError('One or more exact historical screened IDs are missing from Outreach candidates')
    return {
        'job_id': SOURCE_JOB_ID, 'run_version': SOURCE_RUN_VERSION,
        'status': job['status'], 'candidate_ids': ids,
        'candidate_count': len(ids), 'kind_breakdown': {
            'company': breakdown['business_company'], 'hotel': breakdown['hotel_property']},
        'manifest_sha256': EXPECTED_SOURCE_SHA256,
        'criteria': dict(SCREENING_CRITERIA),
    }


def execution_scope(connection, source_ids: list[str]) -> dict:
    """Remove only paid-execution prohibitions; keep the full source set immutable."""
    rows = connection.execute(select(candidates.c.id, candidates.c.suppressed, candidates.c.fixture,
        candidates.c.personalization_outcome).where(candidates.c.id.in_(source_ids))).mappings().all()
    by_id = {row['id']: dict(row) for row in rows}
    if set(by_id) != set(source_ids):
        raise ValueError('The exact original-screen candidate set no longer resolves in Outreach')
    invited = set(connection.execute(select(invitations.c.candidate_id).where(
        invitations.c.candidate_id.in_(source_ids))).scalars())
    attempts = connection.execute(select(personalization_attempts.c.candidate_id,
        personalization_attempts.c.status).where(personalization_attempts.c.candidate_id.in_(source_ids))).all()
    attempt_statuses: dict[str, set[str]] = {}
    for candidate_id, status in attempts:
        attempt_statuses.setdefault(candidate_id, set()).add(status)
    execution_ids = []
    excluded: dict[str, list[str]] = {}
    for ident in source_ids:
        row = by_id[ident]
        reasons = []
        if row['suppressed']:
            reasons.append('SUPPRESSED')
        if row['fixture']:
            reasons.append('OWNER_CONTROLLED_FIXTURE')
        if ident in invited:
            reasons.append('PRIOR_INVITATION')
        if row['personalization_outcome'] == 'NO_AUTOMATIC_RETRY':
            reasons.append('NO_AUTOMATIC_RETRY')
        if attempt_statuses.get(ident, set()) & {'UNKNOWN_SPEND', 'RESERVED'}:
            reasons.append('UNRESOLVED_PROVIDER_SPEND')
        if reasons:
            excluded[ident] = reasons
        else:
            execution_ids.append(ident)
    counts = {}
    for reasons in excluded.values():
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return {'candidate_ids': execution_ids, 'candidate_count': len(execution_ids),
        'candidate_ids_sha256': ids_sha256(execution_ids), 'excluded': excluded,
        'excluded_reason_counts': counts}


def terminal_summary(db, source_ids: list[str], execution_ids: list[str]) -> dict:
    """Classify all source members without treating legacy READY as semantic PASS."""
    from datetime import datetime, timezone
    from .db import events, invitations, personalization_attempts
    from .personalization_batch import eligibility

    with db.transaction() as c:
        rows=c.execute(select(candidates.c.id,candidates.c.kind,candidates.c.suppressed,candidates.c.fixture,
            candidates.c.website_status,candidates.c.qualification,candidates.c.personalization_analysis_version,
            candidates.c.personalization_quality_status,candidates.c.personalization_outcome,
            candidates.c.personalization_retry_after,candidates.c.personalization_attempts
        ).where(candidates.c.id.in_(source_ids))).mappings().all()
        by_id={row['id']:dict(row) for row in rows}
        if set(by_id)!=set(source_ids):
            raise ValueError('Cannot report a terminal campaign state with missing source candidates')
        invited=set(c.execute(select(invitations.c.candidate_id).where(
            invitations.c.candidate_id.in_(source_ids))).scalars())
        attempt_rows=c.execute(select(personalization_attempts.c.candidate_id,
            personalization_attempts.c.status).where(personalization_attempts.c.candidate_id.in_(source_ids))).all()
        statuses: dict[str,set[str]]={}
        for ident,status in attempt_rows: statuses.setdefault(ident,set()).add(status)
        now_dt=datetime.now(timezone.utc)
        pass_ids={ident for ident,row in by_id.items() if row['personalization_analysis_version']=='semantic-openai-v1'
            and row['personalization_quality_status']=='PASS'}
        fail_ids={ident for ident,row in by_id.items() if row['personalization_analysis_version']=='semantic-openai-v1'
            and row['personalization_quality_status']=='FAIL'}
        unresolved={ident for ident,values in statuses.items() if values & {'UNKNOWN_SPEND','RESERVED'}}
        suppressed={ident for ident,row in by_id.items() if row['suppressed']}
        fixtures={ident for ident,row in by_id.items() if row['fixture']}
        no_retry={ident for ident,row in by_id.items() if row['personalization_outcome']=='NO_AUTOMATIC_RETRY'}
        blocked_invited=invited
        identity_blocked={ident for ident,row in by_id.items() if row['website_status']!='VERIFIED'
            or row['qualification'] not in {'ELIGIBLE','QUALIFIED'}}
        future_retry=set()
        exhausted_retry=set()
        for ident,row in by_id.items():
            if row['personalization_outcome']!='TRANSIENT_HTTP': continue
            attempts=int(row['personalization_attempts'] or 0)
            try:
                retry=datetime.fromisoformat(row['personalization_retry_after'] or '')
                retry=retry.replace(tzinfo=timezone.utc) if retry.tzinfo is None else retry
            except (TypeError,ValueError):
                retry=None
            if attempts<5 and retry and retry>now_dt and ident not in unresolved:
                future_retry.add(ident)
            elif attempts>=5:
                exhausted_retry.add(ident)
        terminal_outcome={ident for ident,row in by_id.items() if row['personalization_outcome'] not in
            (None,'TRANSIENT_HTTP','SEMANTIC_READY') and ident not in no_retry}
        protected=unresolved|suppressed|fixtures|blocked_invited|no_retry
        blocked=protected|identity_blocked|exhausted_retry|terminal_outcome
        eligible_now=int(c.scalar(select(func.count()).select_from(candidates).where(
            eligibility(execution_ids))) or 0) if execution_ids else 0
    terminal_ids=pass_ids|fail_ids|blocked|future_retry
    pending_ids=set(source_ids)-terminal_ids
    return {
        'source_candidate_count':len(source_ids),'execution_candidate_count':len(execution_ids),
        'semantic_pass':len(pass_ids),'semantic_fail':len(fail_ids),
        'eligible_now':eligible_now,'future_retry':len(future_retry),
        'suppressed':len(suppressed),'owner_controlled_fixtures':len(fixtures),
        'prior_invitations':len(blocked_invited),'unresolved_spend':len(unresolved),
        'no_automatic_retry':len(no_retry),'identity_or_qualification_blocked':len(identity_blocked),
        'transient_retry_exhausted':len(exhausted_retry),'other_terminal_failure':len(terminal_outcome),
        'blocked_or_skipped':len(blocked),'pending_count':len(pending_ids),
        'provider_attempts':len(attempt_rows),
    }
