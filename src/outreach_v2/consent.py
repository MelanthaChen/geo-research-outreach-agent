"""Authenticated Sheets responses, independent decisions and immutable scope binding."""
import uuid
from sqlalchemy import select
from .db import invitations,submissions,jobs,candidates,events,now
from .migrate import dump,parse,sha
from .template import host,EMAIL
from outreach_agent.google_forms_integration import parse_sheet_row

REQUIRED_HEADERS={
 'Invitation Reference','Is your company interested in participating in this research?',
 'Does your company authorize our research team to conduct the research described above?',
 'Full Name of the Authorized Company Representative','Job Title of the Authorized Company Representative',
 'Business Email Address of the Authorized Company Representative','Timestamp',
 'Do you confirm that you are authorized to provide this permission on behalf of your business?'}

def record_owner_identity_binding(db,invitation_id,verified_respondent_email,operator_email):
    """Persist an audited binding between one invitation and a verified account.

    The ADMIN action records the intended respondent account; authorization is
    still granted only when Google Forms later verifies that exact account on
    the response and the response carries both affirmative consent answers.
    """
    email=str(verified_respondent_email or '').strip().lower()
    if not EMAIL.fullmatch(email):raise ValueError('A valid Google Forms respondent email is required')
    with db.transaction(serialized=True) as c:
        inv=c.execute(select(invitations).where(invitations.c.id==invitation_id)).mappings().first()
        if not inv:raise LookupError('Invitation not found')
        candidate=c.execute(select(candidates).where(candidates.c.id==inv['candidate_id'])).mappings().one()
        authority=parse(inv['authority']) or {}
        if (authority.get('website')!=inv['website'] or candidate['website']!=inv['website']
                or not inv['reference'] or not inv['recipient']):
            raise PermissionError('Invitation, candidate website, and immutable invitation scope must match before identity binding')
        authority=parse(inv['authority']) or {};binding={
            'id':'owner-binding-'+sha(inv['id']+':'+email)[:20],
            'candidate_id':candidate['id'],'invitation_id':inv['id'],'invitation_reference':inv['reference'],
            'website':inv['website'],'invitation_recipient':inv['recipient'],
            'verified_respondent_email':email,'identity_source':'GOOGLE_FORMS_VERIFIED_RESPONDENT_EMAIL',
            'operator':str(operator_email or '').strip().lower(),
            'authorization':'EXPLICIT_OWNER_CONTROLLED_STAGING_BINDING','recorded_at':now()}
        prior=authority.get('owner_identity_binding')
        if prior:
            if {k:v for k,v in prior.items() if k!='recorded_at'}!={k:v for k,v in binding.items() if k!='recorded_at'}:
                raise PermissionError('A conflicting owner identity binding is already recorded')
            return 'ALREADY_RECORDED'
        authority['owner_identity_binding']=binding
        c.execute(invitations.update().where(invitations.c.id==inv['id']).values(authority=dump(authority)))
        c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=inv['id'],event='OWNER_TEST_IDENTITY_BOUND',
            detail=dump(binding),created_at=now()))
        return 'RECORDED'

def _upsert_research_job(c,db,inv,submission_id,stamp):
    db.upsert(c,jobs,[dict(id='job-'+sha(inv['id'])[:24],invitation_id=inv['id'],submission_id=submission_id,
        status='QUEUED',stage='RESEARCH',idempotency_key='outreach-v2:'+inv['id'],property_id=None,audit_id=None,
        opportunity_ids=None,engine_job_id=None,result_id=None,result=None,attempts=0,next_attempt_at=stamp,
        lease_until=None,lease_token=None,error=None,created_at=stamp,started_at=None,completed_at=None,updated_at=stamp)],update=False)

def process(db,row,verified_identity=None):
    parsed=parse_sheet_row(row)
    # Identity is the immutable content fingerprint, not a spreadsheet row number.
    verification=verified_identity or {}
    ident='submission-'+sha(dump({'answers':row,'verified_identity':verification}));stamp=now()
    with db.transaction(serialized=True) as c:
        prior=c.execute(select(submissions.c.status).where(submissions.c.id==ident)).first()
        if prior:
            return 'DUPLICATE'
        inv=c.execute(select(invitations).where(invitations.c.reference==parsed.invitation_reference)).mappings().first()
        status='BLOCKED_REFERENCE';reason='Unknown exact invitation reference'
        if inv:
            candidate=c.execute(select(candidates).where(candidates.c.id==inv['candidate_id'])).mappings().one()
            authority=parse(inv['authority']);email=parsed.contact_email.lower()
            known=c.execute(select(submissions).where(submissions.c.invitation_id==inv['id'])).mappings().all()
            decisions={(r['interest'],r['authorization']) for r in known}
            decision=(parsed.research_interest,parsed.research_authorization)
            if inv['consent_status'] in {'BLOCKED_CONFLICT','WITHDRAWN'}:
                status=inv['consent_status'];reason='Prior conflicting or withdrawn authorization remains blocked'
            elif decisions and decision not in decisions:
                status='BLOCKED_CONFLICT';reason='Conflicting interest/authorization submissions'
            elif parsed.research_interest not in {'YES','NO'} or parsed.research_authorization not in {'YES','NO'}:
                status='BLOCKED_CONSENT';reason='Both independent YES/NO answers are required'
            elif decision!=('YES','YES'):
                status='DECLINED';reason='Research requires both interest and authorization YES'
            elif candidate['suppressed']:
                status='WITHDRAWN';reason='Candidate suppressed or authorization withdrawn'
            elif not parsed.representative_name or not parsed.representative_role or not EMAIL.fullmatch(email):
                status='BLOCKED_IDENTITY';reason='Representative identity is incomplete'
            elif not parsed.submitted_at:
                status='BLOCKED_IDENTITY';reason='Submission timestamp is missing'
            # Resolve company name and exact site from the immutable invitation reference.
            # Removed respondent fields were self-reported and are not part of the contract.
            elif str(row.get('Do you confirm that you are authorized to provide this permission on behalf of your business?') or '').strip().casefold() not in {'yes','yes, i confirm'}:
                status='BLOCKED_IDENTITY';reason='Representative has not affirmed authority to provide permission'
            elif authority.get('website')!=inv['website'] or host(candidate['website'])!=host(inv['website']):
                status='BLOCKED_SCOPE';reason='Current company website does not match the invited scope'
            elif (binding:=authority.get('owner_identity_binding') or {}) and \
                    binding.get('candidate_id')==candidate['id'] and binding.get('invitation_id')==inv['id'] and \
                    binding.get('invitation_reference')==inv['reference'] and binding.get('website')==inv['website'] and \
                    binding.get('invitation_recipient','').lower()==inv['recipient'].lower() and \
                    binding.get('verified_respondent_email','').lower()==str(verification.get('email') or '').lower() and \
                    binding.get('identity_source')=='GOOGLE_FORMS_VERIFIED_RESPONDENT_EMAIL' and \
                    binding.get('authorization')=='EXPLICIT_OWNER_CONTROLLED_STAGING_BINDING' and \
                    bool(binding.get('recorded_at')) and \
                    verification.get('provider')=='GOOGLE_FORMS_VERIFIED' and \
                    verification.get('email_collection_type')=='VERIFIED' and bool(verification.get('response_id')):
                status='AUTHORIZED';reason='Exact audited owner-test binding to Google Forms verified respondent identity'
            elif verification.get('provider')=='GOOGLE_FORMS_VERIFIED' and verification.get('email_collection_type')=='VERIFIED':
                verified_email=str(verification.get('email') or '').strip().lower()
                if (verified_email and verification.get('response_id') and verified_email==email==inv['recipient'].lower()):
                    status='AUTHORIZED';reason='Google Forms verified the respondent account; it matches the original first-party invitation recipient and exact website scope'
                else:
                    status='BLOCKED_IDENTITY';reason='Verified Google respondent account does not match the representative email and original invitation recipient'
            else:
                # The form's business-email question is self-reported. A match to
                # that field alone or possession of a public reference is not proof.
                status='BLOCKED_IDENTITY';reason='Form email is self-reported; Google Forms has not independently verified the respondent account'
            if known and decision==('YES','YES') and any(r['status']=='AUTHORIZED' for r in known) and status=='AUTHORIZED':
                status='DUPLICATE_CONSENT';reason='Authorization already bound; no additional research job'
        c.execute(submissions.insert().values(id=ident,invitation_id=inv['id'] if inv else None,reference=parsed.invitation_reference,
            interest=parsed.research_interest,authorization=parsed.research_authorization,
            representative_name=parsed.representative_name,representative_role=parsed.representative_role,
            representative_email=parsed.contact_email,submitted_at=parsed.submitted_at,received_at=stamp,
            identity_provider=verification.get('provider'),identity_verified_email=verification.get('email'),
            identity_reference=verification.get('response_id'),status=status,reason=reason,
            raw=dump({'answers':row,'verified_identity':verification})))
        if inv and status!='DUPLICATE_CONSENT':
            c.execute(invitations.update().where(invitations.c.id==inv['id']).values(consent_status=status))
            if status=='AUTHORIZED':
                _upsert_research_job(c,db,inv,ident,stamp)
            else:
                c.execute(jobs.update().where(jobs.c.invitation_id==inv['id']).values(status='BLOCKED_CONSENT',error=reason,updated_at=stamp))
            detail={'reason':reason}
            if status=='AUTHORIZED' and verification.get('provider')=='GOOGLE_FORMS_VERIFIED':
                detail.update({'response_id':verification.get('response_id'),'verified_respondent_email':verification.get('email'),
                    'invitation_recipient':inv['recipient'],'website':inv['website'],'identity_binding_id':(authority.get('owner_identity_binding') or {}).get('id')})
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=inv['id'],event=status,detail=dump(detail),created_at=stamp))
        return status
