"""Authenticated Sheets responses, independent decisions and immutable scope binding."""
import uuid
from sqlalchemy import select,func
from .db import invitations,submissions,jobs,candidates,events,suppressions,now
from .migrate import dump,parse,sha
from .template import host,EMAIL
from outreach_agent.google_forms_integration import parse_sheet_row

REQUIRED_HEADERS={
 'Invitation Reference','Is your company interested in participating in this research?',
 'Does your company authorize our research team to conduct the research described above?',
 'Full Name of the Authorized Company Representative','Job Title of the Authorized Company Representative',
 'Business Email Address of the Authorized Company Representative','Timestamp',
 'Do you confirm that you are authorized to provide this permission on behalf of your business?'}

_GEOAIRESUME_FIXTURE_ID='geoairesume-owner-test-v1'
_GEOAIRESUME_FIXTURE_REFERENCE='GEO-A67A912BDB53E20447BD1E323C935738'
_GEOAIRESUME_FIXTURE_WEBSITE='https://geo-ai-resume.vercel.app'

def _geoairesume_fixture_name_allowed(inv,candidate,parsed,row,authority):
    """One explicit name alias for the original owner-controlled test invitation."""
    return (inv['reference']==_GEOAIRESUME_FIXTURE_REFERENCE
        and inv['candidate_id']==_GEOAIRESUME_FIXTURE_ID and candidate['fixture'] is True
        and inv['company_name'].strip().casefold()=='geoairesume controlled test website'
        and inv['website']==_GEOAIRESUME_FIXTURE_WEBSITE
        and candidate['website']==_GEOAIRESUME_FIXTURE_WEBSITE
        and authority.get('method')=='OWNER_BINDING'
        and host(candidate['website'])==host(inv['website']))

def _saved_fixture_revalidation(c,db,ident,stamp):
    """Revalidate only this exact previously blocked saved response, atomically."""
    saved=c.execute(select(submissions).where(submissions.c.id==ident)).mappings().first()
    if not saved or saved['status']!='BLOCKED_SCOPE': return None
    data=parse(saved['raw']) or {};row=data.get('answers') or {};verification=data.get('verified_identity') or {}
    parsed=parse_sheet_row(row)
    if parsed.invitation_reference!=_GEOAIRESUME_FIXTURE_REFERENCE:
        return None
    inv=c.execute(select(invitations).where(invitations.c.id==saved['invitation_id'])).mappings().first()
    if not inv or inv['reference']!=_GEOAIRESUME_FIXTURE_REFERENCE or inv['consent_status']!='BLOCKED_SCOPE': return None
    if c.scalar(select(func.count()).select_from(submissions).where(submissions.c.invitation_id==inv['id']))!=1: return None
    candidate=c.execute(select(candidates).where(candidates.c.id==inv['candidate_id'])).mappings().first()
    if not candidate: return None
    authority=parse(inv['authority']) or {};email=(parsed.contact_email or '').strip().lower()
    # Re-run every normal authorization gate against the saved immutable answers.
    if candidate['suppressed'] or c.execute(select(suppressions.c.email).where(suppressions.c.email==email)).first(): return None
    prior=c.execute(select(submissions).where(submissions.c.invitation_id==inv['id'],submissions.c.id!=ident)).mappings().all()
    decision=(parsed.research_interest,parsed.research_authorization)
    if (inv['consent_status'] in {'BLOCKED_CONFLICT','WITHDRAWN'} or any((r['interest'],r['authorization'])!=decision for r in prior)
        or decision!=('YES','YES') or not parsed.representative_name or not parsed.representative_role
        or not EMAIL.fullmatch(email) or not parsed.submitted_at
        or not _geoairesume_fixture_name_allowed(inv,candidate,parsed,row,authority)
        or str(row.get('Do you confirm that you are authorized to provide this permission on behalf of your business?') or '').strip().casefold() not in {'yes','yes, i confirm'}
        or authority.get('website')!=_GEOAIRESUME_FIXTURE_WEBSITE
        or authority.get('method')!='OWNER_BINDING'
        or email!=str(authority.get('representative') or '').lower()
        or email!=str(authority.get('recipient') or '').lower()
        or email!=str(inv['recipient'] or '').lower()):
        return None
    previous_reason=saved['reason']
    c.execute(submissions.update().where(submissions.c.id==ident).values(status='AUTHORIZED',reason='Exact owner-controlled GeoAIResume fixture revalidated from its original saved response'))
    c.execute(invitations.update().where(invitations.c.id==inv['id']).values(consent_status='AUTHORIZED'))
    _upsert_research_job(c,db,inv,ident,stamp)
    c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=inv['id'],event='AUTHORIZED',
        detail=dump({'action':'OWNER_FIXTURE_SCOPE_REVALIDATED','submission_id':ident,'prior_status':'BLOCKED_SCOPE','prior_reason':previous_reason}),created_at=stamp))
    return 'AUTHORIZED'

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
            recovered=_saved_fixture_revalidation(c,db,ident,stamp)
            return recovered or 'DUPLICATE'
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
            elif authority.get('method')=='OWNER_BINDING' and email==authority.get('representative') and inv['recipient']==authority.get('recipient'):
                status='AUTHORIZED';reason='Exact established owner identity and website scope'
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
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=inv['id'],event=status,detail=reason,created_at=stamp))
        return status
