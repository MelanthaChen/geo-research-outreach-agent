from __future__ import annotations
import base64,binascii,hashlib,json,secrets,uuid
import re
from datetime import datetime,timedelta,timezone
from urllib.parse import urlparse
from sqlalchemy import select,func,and_,or_,exists,case
from .db import candidates,contacts,invitations,batches,jobs,submissions,reports,deliveries,suppressions,events,worker_runs,questionnaires,invitation_corrections,email_templates,now
from .template import validate,render,VERSION,BULK_SUBJECT,EMAIL,same_site
from .migrate import dump,parse,sha,unpack
from .owner_test_fixtures import OWNER_TEST_FIXTURES, SYNTHETIC_CLASSIFICATION, synthetic_fixture_copy
from outreach_agent.google_forms_integration import prefilled_form_url

FILTERS={'kind','industry','location','contact','primary_status','completed_personalization','q'}
CONTACT_VERIFICATION_MAX_AGE=timedelta(days=180)
_UNSUPPORTED_PURPOSE=('press','media','public relations','newsletter','subscribe','career','recruit','employment','jobs',
    'privacy','legal','reservation','booking','concierge','event','catering','wholesale','partner','sales','education',
    'training','order','support','helpdesk')
_GENERAL_PURPOSE_PHRASES=('general business','business inquiries','general inquiries','general contact','business contact','general office')
_RESTRICTED_LOCALPARTS=('press','media','pr','newsletter','subscribe','career','careers','recruit','jobs','privacy','legal',
    'reservation','reservations','booking','concierge','events','catering','wholesale','partner','sales','education','training','orders')
_PLACEHOLDER_HOSTS=('example.com','example.org','example.net','test.com','test.org','mysite.com','email.com','invalid','localhost')

def exact_email_in_evidence(email,evidence):
    email=(email or '').strip().casefold()
    if not EMAIL.fullmatch(email) or not isinstance(evidence,str):return False
    pattern=r'(?<![a-z0-9_.+-])'+re.escape(email)+r'(?![a-z0-9_-])(?!\.[a-z0-9-])'
    return re.search(pattern,evidence,re.I) is not None

def supported_general_business_purpose(purpose,email=None):
    text=(purpose or '').strip().casefold()
    if not any(phrase in text for phrase in _GENERAL_PURPOSE_PHRASES):return False
    if any(term in text for term in _UNSUPPORTED_PURPOSE):return False
    local=(email or '').split('@',1)[0].casefold()
    if any(local==term or local.startswith(term+'.') or local.startswith(term+'_') or local.startswith(term+'-') for term in _RESTRICTED_LOCALPARTS):return False
    host=(email or '').rsplit('@',1)[-1].casefold().rstrip('.')
    if host in _PLACEHOLDER_HOSTS or any(host.endswith('.'+suffix) for suffix in ('example','test','invalid','localhost')):return False
    return True

def contact_verification_is_fresh(contact,*,at=None):
    if contact.get('verification')!='VERIFIED' or not contact.get('verified_at'):return False
    try:
        stamp=datetime.fromisoformat(str(contact['verified_at']).replace('Z','+00:00'))
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
    except (TypeError,ValueError):return False
    current=at or datetime.now(timezone.utc)
    if current.tzinfo is None:current=current.replace(tzinfo=timezone.utc)
    return current-stamp<=CONTACT_VERIFICATION_MAX_AGE and stamp<=current+timedelta(minutes=5)

def contact_is_authorized(row,contact,*,suppressed=False):
    email=(contact.get('email') or '').strip().casefold()
    if not EMAIL.fullmatch(email) or contact.get('suppressed') or suppressed:return False
    if row.get('fixture'):
        return (contact.get('verification') in {'OWNER_AUTHORIZED','VERIFIED'}
            and same_site(row.get('website'),contact.get('source_url')))
    if not same_site(row.get('website'),contact.get('source_url')):return False
    if not contact_verification_is_fresh(contact):return False
    if not exact_email_in_evidence(email,contact.get('evidence')):return False
    if not supported_general_business_purpose(contact.get('purpose'),email):return False
    return True

def contact_assessment(row,contact,*,suppressed=False):
    matched=exact_email_in_evidence(contact.get('email'),contact.get('evidence'))
    fresh=contact_verification_is_fresh(contact)
    if contact.get('verification')=='OWNER_AUTHORIZED' and row.get('fixture'):
        state='VERIFIED'
    elif fresh and matched:state='VERIFIED'
    elif matched:state='EVIDENCE_MATCHED'
    else:state='DISCOVERED'
    authorized=contact_is_authorized(row,contact,suppressed=suppressed)
    return {'contact_state':state,'evidence_matched':matched,'recipient_authorized':authorized,
        'authorization_block_reason':None if authorized else '; '.join(contact_block_reasons_for(contact,row,suppressed=suppressed))}

def contact_block_reasons_for(contact,row,*,suppressed=False):
    reasons=[];email=(contact.get('email') or '').strip().casefold()
    if contact.get('verification') not in {'VERIFIED','OWNER_AUTHORIZED'}:reasons.append('Contact is not verified')
    if contact.get('verification')=='VERIFIED' and not contact_verification_is_fresh(contact):reasons.append('Verification is missing or stale')
    if not exact_email_in_evidence(email,contact.get('evidence')):reasons.append('Exact address is absent from preserved first-party evidence')
    if not supported_general_business_purpose(contact.get('purpose'),email):reasons.append('Purpose is not a supported general business channel')
    if contact.get('suppressed') or suppressed:reasons.append('Contact is suppressed')
    if not same_site(row.get('website'),contact.get('source_url')):reasons.append('Source is not the verified business website')
    return reasons

def _sql_evidence_matches_contact():
    pattern=(r'(^|[^a-z0-9_.+-])' + func.lower(contacts.c.email) + r'($|[^a-z0-9_.-]|[.](?:$|[\s,;:!?)]))')
    return func.lower(func.coalesce(contacts.c.evidence,'')).regexp_match(pattern)

def email_contacts():
    # This prefilter mirrors the material authorization gates used at preview and commit.
    first_party=or_(*[contacts.c.source_url.like(prefix+candidates.c.domain+'/%') for prefix in ('https://','http://','https://www.','http://www.')],
                    *[contacts.c.source_url==prefix+candidates.c.domain for prefix in ('https://','http://','https://www.','http://www.')])
    purpose=func.coalesce(contacts.c.purpose,'')
    restricted=or_(*[purpose.ilike('%'+term+'%') for term in _UNSUPPORTED_PURPOSE])
    supported=or_(*[purpose.ilike('%'+term+'%') for term in _GENERAL_PURPOSE_PHRASES])
    blocked_local=or_(*[contacts.c.email.ilike(term+'@%') for term in _RESTRICTED_LOCALPARTS])
    placeholder=or_(*[contacts.c.email.ilike('%@'+term) for term in _PLACEHOLDER_HOSTS],
        contacts.c.email.ilike('%@example.%'),contacts.c.email.ilike('%@test.%'))
    fresh_after=(datetime.now(timezone.utc)-CONTACT_VERIFICATION_MAX_AGE).isoformat()
    regular=and_(contacts.c.verification=='VERIFIED',contacts.c.verified_at.is_not(None),contacts.c.verified_at>=fresh_after,
        supported,~restricted,~blocked_local,~placeholder,_sql_evidence_matches_contact())
    fixture_owner=and_(candidates.c.fixture.is_(True),contacts.c.verification=='OWNER_AUTHORIZED')
    return and_(contacts.c.candidate_id==candidates.c.id,contacts.c.email.like('%@%.%'),contacts.c.email.not_like('% %'),contacts.c.suppressed.is_(False),
                or_(regular,fixture_owner),
                or_(first_party,fixture_owner),
                ~exists(select(suppressions.c.email).where(func.lower(suppressions.c.email)==func.lower(contacts.c.email))))

def completed_personalization():
    """The persisted semantic quality result is authoritative; legacy READY is a cache."""
    return and_(candidates.c.personalization_quality_status=='PASS',
        candidates.c.personalization_analysis_version=='semantic-openai-v1',
        candidates.c.personalization_version>0,candidates.c.fixture.is_(False))

def authorized_email_count():
    return select(func.count(func.distinct(func.lower(contacts.c.email)))).where(email_contacts()).correlate(candidates).scalar_subquery()

def has_invitation():
    return exists(select(invitations.c.id).where(invitations.c.candidate_id==candidates.c.id))

def primary_status_expression():
    """Mutually exclusive business status derived from persisted facts, never stored."""
    completed=completed_personalization()
    blocked=or_(candidates.c.suppressed.is_(True),candidates.c.fixture.is_(True),
        candidates.c.website_status.is_distinct_from('VERIFIED'),
        candidates.c.qualification.is_(None),~candidates.c.qualification.in_(['ELIGIBLE','QUALIFIED']),
        candidates.c.personalization_quality_status=='FAIL')
    contact_count=authorized_email_count()
    return case(
        (has_invitation(),'ALREADY_INVITED'),
        (blocked,'BLOCKED'),
        (~completed,'PENDING_PERSONALIZATION'),
        (contact_count==0,'PERSONALIZED_NO_CONTACT'),
        else_='READY_TO_INVITE')

STATUS_LABELS={
    'READY_TO_INVITE':'Ready to Invite',
    'PERSONALIZED_NO_CONTACT':'Personalized · No Contact',
    'ALREADY_INVITED':'Already Invited',
    'PENDING_PERSONALIZATION':'Pending Personalization',
    'BLOCKED':'Blocked',
}

def status_reason(row, status):
    if status=='ALREADY_INVITED': return 'An invitation already exists for this business.'
    if status=='BLOCKED':
        if row.get('suppressed'): return 'This business is suppressed.'
        if row.get('fixture'): return 'This is an owner-controlled test fixture, not a real business record.'
        if row.get('website_status') in {'IDENTITY_MISMATCH','AMBIGUOUS_IDENTITY'}: return 'The website identity does not match this business; verify the official domain.'
        if row.get('website_status')!='VERIFIED': return 'Verify the official website and business identity.'
        if row.get('qualification') not in {'ELIGIBLE','QUALIFIED'}: return 'The business has not passed qualification.'
        if row.get('personalization_quality_status')=='FAIL': return 'Personalization did not pass its evidence quality checks.'
        return 'More than one authorized business email is available; resolve recipient ambiguity.'
    if status=='PENDING_PERSONALIZATION':
        if row.get('personalization_quality_status')=='FAIL': return 'Personalization did not pass its evidence quality checks.'
        return 'Complete and quality-check evidence-backed personalization.'
    if status=='PERSONALIZED_NO_CONTACT': return 'Personalization passed; add one verified, authorized business email to prepare an invitation.'
    return 'Ready for invitation preparation.'

def owner_test_recipient_authorized(c, row, recipient, *, saved_contacts=None, suppressed_emails=None):
    """Recheck the exact fixture's saved owner binding at every invitation gate."""
    fixture=OWNER_TEST_FIXTURES.get(row.get('id'))
    if (not fixture or row.get('fixture') is not True or row.get('website')!=fixture['website']
        or row.get('website_status')!='VERIFIED' or row.get('qualification') not in {'ELIGIBLE','QUALIFIED'}
        or row.get('suppressed') or (recipient or '').strip().casefold()!=fixture['recipient'].casefold()):
        return False
    saved=(saved_contacts if saved_contacts is not None else
        c.execute(select(contacts).where(contacts.c.candidate_id==row['id'])).mappings().all())
    if len(saved)!=1:
        return False
    contact=saved[0]
    if ((contact['email'] or '').strip().casefold()!=fixture['recipient'].casefold()
        or contact['verification']!='OWNER_AUTHORIZED' or contact['suppressed']
        or not same_site(row['website'],contact['source_url'])):
        return False
    if suppressed_emails is not None:return fixture['recipient'].casefold() not in suppressed_emails
    return not bool(c.scalar(select(suppressions.c.email).where(
        func.lower(suppressions.c.email)==fixture['recipient'].casefold())))

def validate_invitation_personalization(c, row, personalization, recipient=None):
    """Apply the ordinary quality gate except for a verified exact owner fixture."""
    synthetic=synthetic_fixture_copy(row,personalization)
    if synthetic and recipient is not None and not owner_test_recipient_authorized(c,row,recipient):
        raise ValueError('Synthetic test copy requires its exact unsuppressed OWNER_AUTHORIZED fixture recipient')
    validate(personalization,row['website'],record=row)
    if not synthetic and row.get('personalization_quality_status')!='PASS':
        raise ValueError('Personalization quality is '+str(row.get('personalization_quality_status') or 'UNVERIFIED')+
            (': '+str(row.get('personalization_quality_errors') or 'quality audit required')
             if row.get('personalization_quality_errors') else ''))
    return synthetic

def predicates(filters):
    unknown=set(filters)-FILTERS
    if unknown:raise ValueError('Unknown filters: '+','.join(sorted(unknown)))
    clauses=[]
    for key,value in filters.items():
        if value in ('',None):continue
        if key=='q':
            term='%'+str(value).replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            clauses.append(or_(candidates.c.name.ilike(term,escape='\\'),candidates.c.website.ilike(term,escape='\\')))
        elif key in {'industry','location'}:clauses.append(getattr(candidates.c,key).ilike('%'+str(value).replace('%','\\%').replace('_','\\_')+'%',escape='\\'))
        elif key=='kind':clauses.append(candidates.c.kind==value)
        elif key=='contact':
            condition=exists(select(contacts.c.id).where(contacts.c.candidate_id==candidates.c.id,
                contacts.c.email!='' if value=='email' else contacts.c.url!=''))
            clauses.append(~condition if value=='none' else condition)
        elif key=='primary_status':
            if value not in STATUS_LABELS: raise ValueError('Unknown primary status')
            clauses.append(primary_status_expression()==value)
        elif key=='completed_personalization' and value=='1':clauses.append(completed_personalization())
    return clauses

def _chunks(values,size=5000):
    for start in range(0,len(values),size):
        yield values[start:start+size]

def list_candidates(db,filters=None,page=1,page_size=50,cursor=None):
    filters=filters or {};page=max(1,page);page_size=max(1,min(page_size,100));clauses=predicates(filters)
    cursor_name=cursor_id=None
    if cursor:
        try:
            if len(cursor)>4096:raise ValueError
            padding='='*((4-len(cursor)%4)%4)
            decoded=json.loads(base64.urlsafe_b64decode((cursor+padding).encode()).decode())
            if not isinstance(decoded,list) or len(decoded)!=2 or not all(isinstance(x,str) for x in decoded):raise ValueError
            cursor_name,cursor_id=decoded
        except (ValueError,TypeError,UnicodeDecodeError,json.JSONDecodeError,binascii.Error) as exc:
            raise ValueError('Invalid candidate pagination cursor') from exc
    with db.transaction() as c:
        status=primary_status_expression()
        grouped=c.execute(select(status.label('status'),func.count().label('count'))
            .select_from(candidates).where(*clauses).group_by(status)).all()
        status_counts={key:0 for key in STATUS_LABELS}
        for item in grouped:
            if item.status in status_counts:status_counts[item.status]=item.count
        total=sum(status_counts.values())
        eligible=status_counts['READY_TO_INVITE']
        completed_count=c.scalar(select(func.count()).select_from(candidates).where(completed_personalization()))
        projected=[column for column in candidates.c if column.name!='personalization']
        query=select(*projected,status.label('primary_status')).where(*clauses).order_by(candidates.c.name,candidates.c.id)
        if cursor_name is not None:
            query=query.where(or_(candidates.c.name>cursor_name,
                and_(candidates.c.name==cursor_name,candidates.c.id>cursor_id)))
        else:
            query=query.offset((page-1)*page_size)
        fetched=[dict(r) for r in c.execute(query.limit(page_size+1)).mappings()]
        has_more=len(fetched)>page_size
        rows=fetched[:page_size]
        ids=[row['id'] for row in rows]
        contacts_by_candidate={ident:[] for ident in ids}
        contact_rows=[]
        invitation_by_candidate={};invitation_ids=[]
        if ids:
            for related in c.execute(select(contacts.c.candidate_id,contacts.c.id,contacts.c.email,contacts.c.url,contacts.c.verification,
                    contacts.c.suppressed,contacts.c.source_url,contacts.c.purpose,contacts.c.evidence,contacts.c.source_checksum,
                    contacts.c.verified_at,contacts.c.verification_reason,contacts.c.authorization_reason)
                    .where(contacts.c.candidate_id.in_(ids)).order_by(contacts.c.candidate_id,contacts.c.email)).mappings():
                item=dict(related);contact_rows.append(item)
                contacts_by_candidate[item['candidate_id']].append(item)
            for related in c.execute(select(invitations.c.id,invitations.c.candidate_id,invitations.c.send_status,
                    invitations.c.consent_status,invitations.c.exported_at)
                    .where(invitations.c.candidate_id.in_(ids))).mappings():
                item=dict(related);invitation_by_candidate.setdefault(item['candidate_id'],item);invitation_ids.append(item['id'])
        email_values=sorted({(item['email'] or '').strip().casefold() for item in contact_rows if item['email']})
        suppressed_emails=set()
        for chunk in _chunks(email_values):
            suppressed_emails.update((value or '').strip().casefold() for value in c.execute(
                select(suppressions.c.email).where(func.lower(suppressions.c.email).in_(chunk))).scalars())
        research_by_invitation={};delivery_by_invitation={}
        if invitation_ids:
            for related in c.execute(select(jobs.c.invitation_id,jobs.c.status,jobs.c.error)
                    .where(jobs.c.invitation_id.in_(invitation_ids))).mappings():
                research_by_invitation[related['invitation_id']]={'status':related['status'],'error':related['error']}
            for related in c.execute(select(jobs.c.invitation_id,deliveries.c.status)
                    .select_from(deliveries).join(reports,deliveries.c.report_id==reports.c.id)
                    .join(jobs,reports.c.job_id==jobs.c.id).where(jobs.c.invitation_id.in_(invitation_ids))).mappings():
                delivery_by_invitation.setdefault(related['invitation_id'],related['status'])
        for row in rows:
            row['status_label']=STATUS_LABELS[row['primary_status']]
            row['status_reason']=status_reason(row,row['primary_status'])
            row.update(contact_readiness(c,row,contact_rows=contacts_by_candidate.get(row['id'],[]),
                suppressed_emails=suppressed_emails))
            row['eligible']=row['primary_status']=='READY_TO_INVITE'
            row['contacts']=[{key:item[key] for key in ('email','url','verification')} for item in contacts_by_candidate.get(row['id'],[])]
            row['invitation']=invitation_by_candidate.get(row['id'])
            if row['invitation']:
                invitation_id=row['invitation']['id']
                row['research']=research_by_invitation.get(invitation_id)
                row['delivery']=delivery_by_invitation.get(invitation_id)
        next_cursor=None
        if has_more and rows:
            payload=json.dumps([rows[-1]['name'],rows[-1]['id']],separators=(',',':')).encode()
            next_cursor=base64.urlsafe_b64encode(payload).decode().rstrip('=')
        return {'rows':rows,'total':total,'eligible_matching':eligible,'completed_personalization_count':completed_count,
            'status_counts':status_counts,'page':page,'page_size':page_size,'next_cursor':next_cursor}

def detail(db,ident):
    with db.transaction() as c:
        row=c.execute(select(candidates).where(candidates.c.id==ident)).mappings().first()
        if not row:raise LookupError('Candidate not found')
        row=dict(row);row['personalization']=parse(row['personalization'])
        # The exact same derived status expression powers the table, filters, and detail view.
        row['primary_status']=c.scalar(select(primary_status_expression()).where(candidates.c.id==ident))
        row['status_label']=STATUS_LABELS[row['primary_status']]
        row['status_reason']=status_reason(row,row['primary_status'])
        row.update(contact_readiness(c,row))
        row['invitation']=c.execute(select(invitations.c.id,invitations.c.send_status,invitations.c.consent_status,
            invitations.c.exported_at).where(invitations.c.candidate_id==ident)).mappings().first()
        row['invitation']=dict(row['invitation']) if row['invitation'] else None
        row['contacts']=[dict(r) for r in c.execute(select(contacts).where(contacts.c.candidate_id==ident)).mappings()]
        for saved_contact in row['contacts']:
            address=(saved_contact.get('email') or '').strip().casefold()
            suppressed=bool(c.scalar(select(suppressions.c.email).where(func.lower(suppressions.c.email)==address))) if address else False
            saved_contact.update(contact_assessment(row,saved_contact,suppressed=suppressed))
        from .db import source_rows
        row['evidence']=[dict(r) for r in c.execute(select(source_rows.c.source_id,source_rows.c.source_key,source_rows.c.raw).where(source_rows.c.candidate_id==ident).limit(30)).mappings()]
        for evidence in row['evidence']:evidence['raw']=unpack(evidence['raw'])
        if not row['evidence']:
            # Lean cloud imports retain the observed invitation evidence and stable
            # archive references without copying every historical source payload.
            row['evidence']=[{'source_id':row.get('source'),'source_key':'Operational evidence summary',
                'raw':dump({'record_type':'LEAN_OPERATIONAL_EVIDENCE_SUMMARY','candidate_id':ident,
                    'source_path':row.get('source'),'source_archive_row_ref':row.get('source_ref'),
                    'source_row_checksum':row.get('source_checksum'),'website':row.get('website'),
                    'website_status':row.get('website_status'),'qualification':row.get('qualification'),
                    'personalization_sources':row['personalization'].get('source_urls',[]),
                    'contacts':[{'source_ref':x.get('source_ref'),'source_checksum':x.get('source_checksum'),
                        'source_url':x.get('source_url'),'evidence':x.get('evidence'),
                        'verification':x.get('verification')} for x in row['contacts']]})}]
        return row

def authorized_contact_emails(c,row,*,contact_rows=None,suppressed_emails=None):
    emails=set()
    source=(contact_rows if contact_rows is not None else
        c.execute(select(contacts).where(contacts.c.candidate_id==row['id'])).mappings())
    for contact in source:
        email=(contact['email'] or '').strip().lower()
        suppressed=(email in suppressed_emails if suppressed_emails is not None else
            bool(c.scalar(select(suppressions.c.email).where(func.lower(suppressions.c.email)==email))))
        if contact_is_authorized(row,contact,suppressed=suppressed):emails.add(email)
    return emails

def authorized_contact_options(c,row,*,contact_rows=None,suppressed_emails=None):
    saved=(contact_rows if contact_rows is not None else
        c.execute(select(contacts).where(contacts.c.candidate_id==row['id'])).mappings())
    result=[]
    for contact in saved:
        email=(contact.get('email') or '').strip().casefold()
        suppressed=(email in suppressed_emails if suppressed_emails is not None else
            bool(c.scalar(select(suppressions.c.email).where(func.lower(suppressions.c.email)==email))))
        if contact_is_authorized(row,contact,suppressed=suppressed):
            result.append({'email':email,'purpose':contact.get('purpose'),'source_url':contact.get('source_url'),
                'source_checksum':contact.get('source_checksum'),'verified_at':contact.get('verified_at'),
                'authorization_reason':contact.get('authorization_reason')})
    return sorted(result,key=lambda item:item['email'])

def contact_block_reasons(c,row,*,saved_contacts=None,suppressed_emails=None):
    saved=(saved_contacts if saved_contacts is not None else
        c.execute(select(contacts).where(contacts.c.candidate_id==row['id'])).mappings().all())
    if not any((x['email'] or '').strip() for x in saved):return ['No business email is saved for this candidate']
    reasons=[]
    for contact in saved:
        email=(contact['email'] or '').strip().lower()
        if not email:continue
        if not EMAIL.fullmatch(email):reasons.append('Saved email address has an invalid format')
        if contact.get('verification') in {None,'','UNKNOWN','PENDING','DISCOVERED','EVIDENCE_MATCHED'}:
            reasons.append('Contact has not completed explicit verification')
        if contact.get('verification')=='VERIFIED' and not contact_verification_is_fresh(contact):
            reasons.append('Contact verification is missing a timestamp or is older than 180 days')
        if not exact_email_in_evidence(email,contact.get('evidence')):
            reasons.append('Exact email address is not present in preserved first-party evidence')
        if not supported_general_business_purpose(contact.get('purpose'),email):
            reasons.append('Contact purpose is not a supported general business channel')
        if contact['suppressed'] or contact['verification']=='SUPPRESSED':reasons.append('Saved contact is suppressed')
        if contact['verification']=='REJECTED':reasons.append('Saved contact verification was rejected')
        if contact['verification']=='INVALID':reasons.append('Saved contact is marked invalid')
        is_suppressed=(email in suppressed_emails if suppressed_emails is not None else
            bool(c.scalar(select(suppressions.c.email).where(func.lower(suppressions.c.email)==email))))
        if is_suppressed:
            reasons.append('Email address appears on the global suppression list')
        if not same_site(row['website'],contact['source_url']) and not (row.get('fixture') and contact['verification']=='OWNER_AUTHORIZED'):
            reasons.append('Contact source is not on the verified business website')
        if any(x in (contact['purpose'] or '').lower() for x in ('privacy','career','recruit','personal','legal','abuse')):
            reasons.append('Contact is for a restricted purpose and cannot be used for invitations')
    return list(dict.fromkeys(reasons)) or ['No saved email passed all contact authorization checks']

def add_contact_from_first_party_page(db,ident,email,source_url,fetcher=None):
    """Persist only an email visibly published on a verified same-site contact page."""
    import html as html_lib
    from bs4 import BeautifulSoup
    from .personalization import fetch_page
    fetcher=fetcher or fetch_page
    normalized=(email or '').strip().casefold()
    if not EMAIL.fullmatch(normalized):raise ValueError('A valid email address is required')
    with db.transaction() as c:
        row=c.execute(select(candidates).where(candidates.c.id==ident)).mappings().first()
        if not row:raise LookupError('Candidate not found')
        row=dict(row)
        if row.get('fixture') or row.get('suppressed') or row.get('website_status')!='VERIFIED':
            raise ValueError('Contact discovery requires a real, unsuppressed candidate with verified website identity')
        if not same_site(row['website'],source_url):raise ValueError('Contact evidence must come from the verified business website')
        if c.scalar(select(suppressions.c.email).where(func.lower(suppressions.c.email)==normalized)):
            raise ValueError('Email is on the global suppression list')
    final_url,raw_html,truncated=fetcher(source_url)
    if truncated or not same_site(row['website'],final_url):raise ValueError('Contact page was truncated or redirected outside the verified business website')
    soup=BeautifulSoup(raw_html,'html.parser')
    mailto={html_lib.unescape(a.get('href','')[7:].split('?',1)[0]).strip().casefold()
        for a in soup.select('a[href^="mailto:"]')}
    for tag in soup(['script','style','template','noscript','svg','iframe','object','embed']):tag.decompose()
    for tag in list(soup.find_all(True)):
        attrs=tag.attrs or {};style=(attrs.get('style') or '').replace(' ','').lower()
        if 'hidden' in attrs or attrs.get('aria-hidden')=='true' or 'display:none' in style or 'visibility:hidden' in style:tag.decompose()
    visible=' '.join(soup.stripped_strings)
    if normalized not in mailto and not re.search(r'(?<![\w.+-])'+re.escape(normalized)+r'(?![\w-])',visible,re.I):
        raise ValueError('The exact email address is not visible on the official page')
    parsed=urlparse(final_url);path=parsed.path.casefold();local=normalized.split('@',1)[0]
    restricted_markers=('privacy','career','careers','recruit','jobs','legal','abuse','press','media','newsletter','subscribe',
        'reservation','booking','concierge','events','catering','wholesale','partner','sales','education','training','orders')
    if any(mark in path for mark in restricted_markers) or any(local==mark or local.startswith(mark+'.') or
        local.startswith(mark+'_') or local.startswith(mark+'-') for mark in restricted_markers):
        raise ValueError('The published address is associated with a restricted contact purpose')
    match=re.search(re.escape(normalized),visible,re.I)
    nearby=visible[max(0,match.start()-180):match.end()+180].casefold() if match else ''
    if not any(term in nearby for term in _GENERAL_PURPOSE_PHRASES):
        raise ValueError('The page does not establish this address as a general business contact')
    host_value=normalized.rsplit('@',1)[-1].rstrip('.')
    if host_value in _PLACEHOLDER_HOSTS or host_value.endswith(('.example','.test','.invalid','.localhost')):
        raise ValueError('Placeholder email domains cannot be authorized')
    evidence=(visible[max(0,match.start()-140):match.end()+140] if match else 'mailto:'+normalized).strip()
    checksum=hashlib.sha256(raw_html.encode('utf-8')).hexdigest();verified_at=now()
    verification_reason='Live bounded same-site page fetch; exact address visible; robots and redirect checks passed.'
    authorization_reason='Exact address matched visible official-site evidence; page context identifies general business inquiries; no suppression matched.'
    contact_id='contact-'+uuid.uuid4().hex
    with db.transaction(serialized=True) as c:
        row=c.execute(select(candidates).where(candidates.c.id==ident)).mappings().first()
        if not row or row['fixture'] or row['suppressed'] or row['website_status']!='VERIFIED' or not same_site(row['website'],final_url):
            raise ValueError('Candidate safety status changed during contact discovery')
        if c.scalar(select(suppressions.c.email).where(func.lower(suppressions.c.email)==normalized)):
            raise ValueError('Email was suppressed during contact discovery')
        existing=c.execute(select(contacts).where(contacts.c.candidate_id==ident,func.lower(contacts.c.email)==normalized)).mappings().first()
        if existing:
            raise ValueError('This candidate already has this contact; existing provenance was preserved')
        c.execute(contacts.insert().values(id=contact_id,candidate_id=ident,kind='EMAIL',email=normalized,
            source_url=final_url,evidence=evidence,verification='VERIFIED',purpose='general business',suppressed=False,
            source_ref='first-party-contact-page:'+parsed.netloc+parsed.path,source_checksum=checksum,
            verified_at=verified_at,verification_reason=verification_reason,authorization_reason=authorization_reason,
            raw=dump({'discovery':'BOUNDED_FIRST_PARTY_CONTACT_PAGE','source_url':final_url,'source_checksum':checksum,
                'verified_at':verified_at,'verification_reason':verification_reason,
                'authorization_reason':authorization_reason,'email_visible_on_page':True,'robots_respected':True})))
        c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=ident,event='FIRST_PARTY_CONTACT_DISCOVERED',
            detail=dump({'contact_id':contact_id,'source_url':final_url,'source_checksum':checksum,
                'email_sha256':hashlib.sha256(normalized.encode()).hexdigest(),'purpose':'general business'}),created_at=now()))
    return {'contact_id':contact_id,'candidate_id':ident,'email':normalized,'source_url':final_url,
        'verification':'VERIFIED','purpose':'general business','source_checksum':checksum,'verified_at':verified_at,
        'verification_reason':verification_reason,'authorization_reason':authorization_reason}

def contact_readiness(c,row,*,contact_rows=None,suppressed_emails=None):
    """An invitation-contact check independent from semantic personalization."""
    emails=authorized_contact_emails(c,row,contact_rows=contact_rows,suppressed_emails=suppressed_emails)
    if not emails:return {'contact_readiness':'MISSING_OR_UNAUTHORIZED','contact_readiness_reason':'No usable first-party business email; review contact evidence, purpose, and suppression.'}
    if len(emails)>1:return {'contact_readiness':'MANUAL_RECIPIENT_SELECTION','contact_readiness_reason':'Multiple authorized first-party contacts; choose a recipient for this invitation.'}
    return {'contact_readiness':'READY','contact_readiness_reason':'One currently authorized first-party business contact.'}

def eligible_recipient(c,row,recipient_choice=None,*,lookup=None):
    reasons=[]
    personalization=parse(row['personalization'])
    synthetic_fixture=synthetic_fixture_copy(row,personalization)
    if not synthetic_fixture and (row.get('personalization_analysis_version')!='semantic-openai-v1' or int(row.get('personalization_version') or 0)<1):
        reasons.append('Personalization is legacy, missing, or outdated; current semantic version required')
    if row['suppressed']:reasons.append('Candidate suppressed or withdrawn')
    if row['website_status']!='VERIFIED':reasons.append('Official website not verified')
    if row['qualification'] not in {'ELIGIBLE','QUALIFIED'}:reasons.append('Candidate not qualified')
    has_prior=(row['id'] in lookup['candidate_ids_with_invitations'] if lookup is not None else
        bool(c.scalar(select(invitations.c.id).where(invitations.c.candidate_id==row['id']))))
    if has_prior:reasons.append('Invitation already exists; download its saved batch')
    try:validate(personalization,row['website'],record=row)
    except ValueError as e:reasons.append(str(e))
    if not synthetic_fixture and row.get('personalization_quality_status')!='PASS':
        reasons.append('Personalization quality is '+str(row.get('personalization_quality_status') or 'UNVERIFIED')+
                       (': '+str(row.get('personalization_quality_errors') or 'quality audit required') if row.get('personalization_quality_errors') else ''))
    saved_contacts=(lookup['contacts_by_candidate'].get(row['id'],[]) if lookup is not None else None)
    emails=authorized_contact_emails(c,row,contact_rows=saved_contacts,
        suppressed_emails=lookup['suppressed_emails'] if lookup is not None else None)
    chosen=(recipient_choice or '').strip().casefold()
    if chosen and chosen not in emails:reasons.append('Selected recipient is not a currently authorized first-party business contact')
    if len(emails)==1:recipient=next(iter(emails))
    elif len(emails)>1 and chosen in emails:recipient=chosen
    else:
        recipient=None
        reasons.append('Multiple recipient emails; choose an authorized first-party recipient' if emails else 'No single valid first-party business email')
        if not emails:reasons.extend(contact_block_reasons(c,row,saved_contacts=saved_contacts,
            suppressed_emails=lookup['suppressed_emails'] if lookup is not None else None))
    if synthetic_fixture:
        expected=OWNER_TEST_FIXTURES[row['id']]['recipient']
        if len(emails)==1 and next(iter(emails))!=expected:
            reasons.append('Synthetic test copy is restricted to its exact owner-authorized recipient')
        elif len(emails)==1 and not owner_test_recipient_authorized(c,row,next(iter(emails)),
                saved_contacts=saved_contacts,
                suppressed_emails=lookup['suppressed_emails'] if lookup is not None else None):
            reasons.append('Synthetic test copy requires its exact unsuppressed OWNER_AUTHORIZED fixture recipient')
    return recipient,reasons

def select_pilot_cohort(c, limit=100):
    """Select an invitation-oriented cohort before paid personalization.

    Require one independently first-party, verified business email and saved
    source provenance at selection time. This check is separate from semantic
    personalization eligibility and does not create invitations.
    """
    if limit != 100: raise ValueError('The professor pilot cohort is fixed at 100 companies')
    rows=c.execute(select(candidates).where(candidates.c.kind=='company',candidates.c.fixture.is_(False),
        candidates.c.website_status=='VERIFIED',candidates.c.qualification.in_(['ELIGIBLE','QUALIFIED']),
        candidates.c.suppressed.is_(False),candidates.c.name.is_not(None),candidates.c.website.is_not(None),
        ~exists(select(invitations.c.id).where(invitations.c.candidate_id==candidates.c.id)))
        .order_by(candidates.c.industry,candidates.c.id)).mappings().all()
    by_industry={}
    from .template import host
    prior_invitations=c.execute(select(invitations.c.website,invitations.c.recipient)).all()
    seen_domains={host(row.website) for row in prior_invitations if host(row.website)}
    seen_recipients={(row.recipient or '').strip().casefold() for row in prior_invitations if row.recipient}
    for item in rows:
        row=dict(item);recipient=None;matching=[]
        for contact in c.execute(select(contacts).where(contacts.c.candidate_id==row['id'])).mappings():
            email=(contact['email'] or '').strip().lower()
            suppressed=bool(c.scalar(select(suppressions.c.email).where(func.lower(suppressions.c.email)==email)))
            if contact_is_authorized(row,contact,suppressed=suppressed):matching.append(dict(contact))
        if len(matching)==1:recipient=matching[0]['email'].strip().lower()
        elif len(matching)>1:continue
        domain=host(row.get('website'))
        if (not recipient or not domain or domain in seen_domains or recipient.casefold() in seen_recipients
            or c.scalar(select(suppressions.c.email).where(suppressions.c.email==recipient))):
            continue
        key=(row.get('industry') or 'Unclassified').strip() or 'Unclassified'
        row['_cohort_contact']=matching[0]
        by_industry.setdefault(key,[]).append((row,recipient,domain))
    # Round-robin industries so a long single-industry run cannot monopolize the cohort.
    selected=[]
    industries=sorted(by_industry)
    while industries and len(selected)<limit:
        remaining=[]
        for industry in industries:
            queue=by_industry[industry]
            while queue:
                row,recipient,domain=queue.pop(0)
                if domain in seen_domains or recipient.casefold() in seen_recipients:
                    continue
                selected.append(row);seen_domains.add(domain);seen_recipients.add(recipient.casefold())
                break
            if queue:remaining.append(industry)
            if len(selected)>=limit:break
        industries=remaining
    return selected

def cohort_member_snapshot(row):
    """Stable, reviewable cohort evidence snapshot; does not mutate candidate."""
    contact=row['_cohort_contact']
    return {
        'criteria_version':'verified-company-cohort-v2',
        'candidate_id':row['id'],'company_name':row['name'],'business_description':row.get('description'),'industry':row.get('industry'),
        'location':row.get('location'),'website':row['website'],'website_status':row['website_status'],
        'qualification_status':row['qualification'],'suppressed':bool(row['suppressed']),
        'outreach_eligibility':'COHORT_ELIGIBLE_NOT_INVITATION_READY',
        'business_email':contact['email'],'contact_kind':contact.get('kind'),'contact_purpose':contact.get('purpose'),
        'contact_verification':contact['verification'],
        'contact_source_url':contact['source_url'],'contact_evidence':contact['evidence'],
        'contact_source_ref':contact.get('source_ref'),'contact_source_checksum':contact.get('source_checksum'),
        'candidate_source':row.get('source'),'candidate_source_ref':row.get('source_ref'),
        'candidate_source_checksum':row.get('source_checksum'),
        'personalization_status':('READY' if row.get('personalization_status')=='READY' and
            row.get('personalization_analysis_version')=='semantic-openai-v1' and int(row.get('personalization_version') or 0)>0 else 'PENDING'),
        'source_personalization_status':row.get('personalization_status') or 'INCOMPLETE',
        'review_status':'PENDING','smb_size_evidence_status':'NOT_ESTABLISHED_IN_STORED_RECORD'
    }

OWNER_BINDINGS={
 'geo-ai-resume.vercel.app':{'recipient':'yuxuan.chen.1031@outlook.com','representative':'yuxuan.chen.1031@outlook.com'},
 'whisker-haven-demo.vercel.app':{'recipient':'yuxaun.chen.1031@outlook.com','representative':'yuxaun.chen.1031@outlook.com'}}

def _batch_selection_query(selection):
    if selection.get('all_matching'):
        return select(candidates).where(*predicates(selection.get('filters') or {})).order_by(candidates.c.id)
    ids=selection.get('ids') or []
    if not ids:raise ValueError('Select at least one company')
    if len(ids)>100000:raise ValueError('Selection too large')
    return select(candidates).where(candidates.c.id.in_(ids)).order_by(candidates.c.id)

def _generation_lookup(c,rows):
    """Load all invitation eligibility dependencies in bounded set queries."""
    ids=[row['id'] for row in rows]
    contacts_by_candidate={ident:[] for ident in ids}
    candidate_ids_with_invitations=set();emails=set();existing_invited_recipients=set()
    for chunk in _chunks(ids):
        for item in c.execute(select(contacts).where(contacts.c.candidate_id.in_(chunk))).mappings():
            item=dict(item);contacts_by_candidate[item['candidate_id']].append(item)
            address=(item.get('email') or '').strip().casefold()
            if address:emails.add(address)
        for item in c.execute(select(invitations.c.candidate_id,invitations.c.recipient)
                .where(invitations.c.candidate_id.in_(chunk))).mappings():
            candidate_ids_with_invitations.add(item['candidate_id'])
            if item['recipient']:existing_invited_recipients.add(item['recipient'].strip().casefold())
    for chunk in _chunks(sorted(emails)):
        for value in c.execute(select(invitations.c.recipient)
                .where(func.lower(invitations.c.recipient).in_(chunk)).distinct()).scalars():
            if value:existing_invited_recipients.add(value.strip().casefold())
    suppressed_emails=set()
    for chunk in _chunks(sorted(emails)):
        suppressed_emails.update((value or '').strip().casefold() for value in c.execute(
            select(suppressions.c.email).where(func.lower(suppressions.c.email).in_(chunk))).scalars())
    return {'contacts_by_candidate':contacts_by_candidate,
        'candidate_ids_with_invitations':candidate_ids_with_invitations,
        'existing_invited_recipients':existing_invited_recipients,
        'suppressed_emails':suppressed_emails}

def _preview_in_transaction(c,selection,context=None):
    rows=[dict(r) for r in c.execute(_batch_selection_query(selection)).mappings()]
    lookup=_generation_lookup(c,rows)
    requested=list(selection.get('ids') or []) if not selection.get('all_matching') else []
    found={r['id'] for r in rows};excluded=[];eligible_ids=[];seen=set()
    questionnaire=c.execute(select(questionnaires).where(questionnaires.c.active.is_(True)).order_by(questionnaires.c.version.desc()).limit(1)).mappings().first()
    invitation_template=c.execute(select(email_templates).where(email_templates.c.active.is_(True),
        email_templates.c.template_kind=='INVITATION').order_by(email_templates.c.version.desc()).limit(1)).mappings().first()
    if context is not None:
        # Reuse the exact config rows read for this reviewed preview rather
        # than issuing duplicate active-template/questionnaire lookups.
        context.update(questionnaire=dict(questionnaire) if questionnaire else None,
            invitation_template=dict(invitation_template) if invitation_template else None)
    questionnaire_fields=parse(questionnaire['field_map']) if questionnaire else {}
    configured_form=questionnaire['form_url'] if questionnaire else None
    reference_entry=questionnaire_fields.get('invitation_reference_entry_id') if questionnaire else None
    from .template import host
    for row in rows:
        choice=(selection.get('recipient_choices') or {}).get(row['id'])
        recipient,reasons=eligible_recipient(c,row,choice,lookup=lookup)
        if recipient in seen:reasons.append('Duplicate recipient within batch')
        if recipient and recipient.casefold() in lookup['existing_invited_recipients']:
            reasons.append('Recipient already has an invitation; resend is not automatic')
        if reasons:
            item={'candidate_id':row['id'],'company':row['name'],'reasons':reasons}
            options=authorized_contact_emails(c,row,contact_rows=lookup['contacts_by_candidate'].get(row['id'],[]),
                suppressed_emails=lookup['suppressed_emails'])
            details=authorized_contact_options(c,row,contact_rows=lookup['contacts_by_candidate'].get(row['id'],[]),
                suppressed_emails=lookup['suppressed_emails'])
            if len(options)>1:
                item['recipient_options']=sorted(options)
                item['recipient_option_details']=details
            excluded.append(item);continue
        p=parse(row['personalization']);reference='GEO-PREVIEW'
        url=prefilled_form_url(reference,form_url=configured_form,entry_id=reference_entry)
        try:
            render(row['name'],row['website'],p,url,reference,record=row,template_config=invitation_template)
        except ValueError as exc:
            excluded.append({'candidate_id':row['id'],'company':row['name'],
                'reasons':['Invalid invitation personalization: '+str(exc)]});continue
        if row['fixture']:
            binding=OWNER_BINDINGS.get(host(row['website']))
            if not binding or binding['recipient']!=recipient:
                excluded.append({'candidate_id':row['id'],'company':row['name'],'reasons':['Owner recipient binding mismatch']});continue
        eligible_ids.append(row['id']);seen.add(recipient)
        if context is not None:context.setdefault('eligible',[]).append((row,recipient))
    for ident in requested:
        if ident not in found:excluded.append({'candidate_id':ident,'company':ident,'reasons':['Candidate not found']})
    selection_core={k:v for k,v in selection.items() if k!='preview_sha256'}
    digest=sha(dump({'selection':selection_core,'eligible_candidate_ids':eligible_ids,'excluded':excluded}))
    if context is not None:context.update(rows=rows,lookup=lookup)
    return {'selected_total':len(requested) if requested else len(rows),'valid_recipient_count':len(eligible_ids),
        'excluded_count':len(excluded),'excluded':excluded,'preview_sha256':digest}

def preview_generation(db,selection):
    if not isinstance(selection,dict) or set(selection)-{'ids','filters','all_matching','recipient_choices'}:raise ValueError('Invalid selection')
    with db.transaction(serialized=True) as c:return _preview_in_transaction(c,selection)

def generate(db,selection,key):
    if not isinstance(key,str) or not 16<=len(key)<=128:raise ValueError('A stable request key is required')
    if set(selection)-{'ids','filters','all_matching','recipient_choices','preview_sha256'}:raise ValueError('Invalid selection')
    if not selection.get('preview_sha256'):raise ValueError('Review the current recipient preview before generating a batch')
    selection_core={k:v for k,v in selection.items() if k!='preview_sha256'}
    selection_hash=sha(dump(selection_core));stamp=now()
    from .package import build_package
    from .template import host
    batch_id='batch-'+uuid.uuid4().hex
    with db.transaction(serialized=True) as c:
        prior=c.execute(select(batches).where(batches.c.request_key==key)).mappings().first()
        if prior:
            if prior['selection_hash']!=selection_hash:raise ValueError('Request key belongs to a different selection')
            result=dict(prior);result['_idempotent_replay']=True;return result
        context={};review=_preview_in_transaction(c,selection,context=context)
        if review['preview_sha256']!=selection['preview_sha256']:
            raise ValueError('Recipient eligibility changed after review; refresh the preview before generating')
        if review['valid_recipient_count']==0:
            raise ValueError('No eligible invitations: '+dump(review['excluded'])+'; no invitations were created')
        questionnaire=context.get('questionnaire')
        invitation_template=context.get('invitation_template')
        config_fingerprint=sha(dump({'questionnaire':questionnaire,'invitation_template':invitation_template}))
    # Render and package outside the database transaction. The following short
    # commit rechecks the preview and active configuration before inserting.
    questionnaire_fields=parse(questionnaire['field_map']) if questionnaire else {}
    configured_form=questionnaire['form_url'] if questionnaire else None
    reference_entry=questionnaire_fields.get('invitation_reference_entry_id') if questionnaire else None
    generated=[];excluded=list(review['excluded'])
    for row,recipient in context.get('eligible',[]):
        p=parse(row['personalization']);invitation_id='inv-'+uuid.uuid4().hex
        reference='GEO-'+secrets.token_hex(16).upper()
        url=prefilled_form_url(reference,form_url=configured_form,entry_id=reference_entry)
        try:subject,body=render(row['name'],row['website'],p,url,reference,record=row,template_config=invitation_template)
        except ValueError as exc:
            excluded.append({'candidate_id':row['id'],'company':row['name'],'reasons':['Invalid invitation personalization: '+str(exc)]});continue
        authority={'method':'INVITATION_EMAIL_AND_FIRST_PARTY_DOMAIN','website':row['website']}
        if row['fixture']:
            binding=OWNER_BINDINGS.get(host(row['website']))
            if not binding or binding['recipient']!=recipient:
                excluded.append({'candidate_id':row['id'],'company':row['name'],'reasons':['Owner recipient binding mismatch']});continue
            authority={'method':'OWNER_BINDING','website':row['website'],**binding}
        generated.append(dict(id=invitation_id,candidate_id=row['id'],batch_id=batch_id,reference=reference,recipient=recipient,
            website=row['website'],company_name=row['name'],subject=subject,body=body,form_url=url,
            template_version=(f"invitation-template-v{invitation_template['version']}" if invitation_template else VERSION),
            questionnaire_id=questionnaire['id'] if questionnaire else None,
            researcher_name=(invitation_template['researcher_name'] if invitation_template else __import__('os').getenv('OUTREACH_RESEARCHER_NAME')),
            university_affiliation=(invitation_template['university_affiliation'] if invitation_template else __import__('os').getenv('OUTREACH_UNIVERSITY_AFFILIATION')),
            reply_to=(invitation_template['reply_to'] if invitation_template else __import__('os').getenv('OUTREACH_REPLY_TO')),
            sender_name=(invitation_template.get('sender_name') if invitation_template else (__import__('os').getenv('OUTREACH_INVITATION_SENDER_NAME') or __import__('os').getenv('OUTREACH_RESEARCHER_NAME'))),
            sender_email=(invitation_template['sender'] if invitation_template else (__import__('os').getenv('OUTREACH_INVITATION_SENDER') or __import__('os').getenv('OUTREACH_REPLY_TO'))),
            personalization=dump(p),generated_at=stamp,exported_at=stamp,send_status='UNVERIFIED',
            consent_status='NOT_RECEIVED',authority=dump(authority)))
    if not generated:raise ValueError('No eligible invitations: '+dump(excluded))
    package=build_package(generated,excluded,BULK_SUBJECT)
    with db.transaction(serialized=True) as c:
        prior=c.execute(select(batches).where(batches.c.request_key==key)).mappings().first()
        if prior:
            if prior['selection_hash']!=selection_hash:raise ValueError('Request key belongs to a different selection')
            result=dict(prior);result['_idempotent_replay']=True;return result
        current=_preview_in_transaction(c,selection)
        if current['preview_sha256']!=selection['preview_sha256']:
            raise ValueError('Recipient eligibility changed while invitations were prepared; refresh the preview before generating')
        latest_questionnaire=c.execute(select(questionnaires).where(questionnaires.c.active.is_(True)).order_by(questionnaires.c.version.desc()).limit(1)).mappings().first()
        latest_template=c.execute(select(email_templates).where(email_templates.c.active.is_(True),
            email_templates.c.template_kind=='INVITATION').order_by(email_templates.c.version.desc()).limit(1)).mappings().first()
        latest_fingerprint=sha(dump({'questionnaire':dict(latest_questionnaire) if latest_questionnaire else None,
            'invitation_template':dict(latest_template) if latest_template else None}))
        if latest_fingerprint!=config_fingerprint:
            raise ValueError('The active invitation template or questionnaire changed while preparing; review again')
        c.execute(batches.insert().values(id=batch_id,request_key=key,selection_hash=selection_hash,created_at=stamp,
            bulk_subject=BULK_SUBJECT,pilot_review_status='PENDING_REVIEW'))
        c.execute(invitations.insert(),generated)
        c.execute(batches.update().where(batches.c.id==batch_id).values(package=package,excluded=dump(excluded),exported_at=stamp))
        c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=batch_id,event='INVITATION_RECIPIENT_REVIEW_CONFIRMED',
            detail=dump({'selected_total':review['selected_total'],'valid_recipient_count':len(generated),
                'generated_count':len(generated),'skipped_count':len(excluded),'failed_count':0,
                'preview_sha256':review['preview_sha256']}),created_at=stamp))
        c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=batch_id,event='EXPORTED',detail=dump({'count':len(generated),'excluded':len(excluded)}),created_at=stamp))
        return dict(c.execute(select(batches).where(batches.c.id==batch_id)).mappings().one())

def batch_by_request_key(db,key):
    if not isinstance(key,str) or not 16<=len(key)<=128:raise ValueError('Invalid request key')
    with db.transaction() as c:
        row=c.execute(select(batches).where(batches.c.request_key==key)).mappings().first()
        if not row:raise LookupError('No saved invitation batch exists for this request key')
        return dict(row)

def dashboard(db):
    with db.transaction() as c:
        run=c.execute(select(worker_runs).order_by(worker_runs.c.started_at.desc()).limit(1)).mappings().first()
        return {'companies':c.scalar(select(func.count()).select_from(candidates).where(candidates.c.kind=='company')),
                'hotels':c.scalar(select(func.count()).select_from(candidates).where(candidates.c.kind=='hotel')),
                'ready_to_invite':c.scalar(select(func.count()).select_from(candidates).where(primary_status_expression()=='READY_TO_INVITE')),
                'completed_personalization':c.scalar(select(func.count()).select_from(candidates).where(completed_personalization())),
                'invitations_exported':c.scalar(select(func.count()).select_from(invitations).where(invitations.c.exported_at.is_not(None))),
                'form_responses':c.scalar(select(func.count()).select_from(submissions)),
                'authorized_jobs':c.scalar(select(func.count()).select_from(jobs)),
                'research_completed':c.scalar(select(func.count()).select_from(jobs).where(jobs.c.completed_at.is_not(None))),
                'reports_accepted':c.scalar(select(func.count()).select_from(deliveries).where(deliveries.c.status=='ACCEPTED')),
                'failures':c.scalar(select(func.count()).select_from(jobs).where(jobs.c.error.is_not(None))),
                'latest_worker_run':({'id':run['id'],'status':run['status'],'started_at':run['started_at'],
                    'detail':parse(run['detail'])} if run else None)}

def create_invitation_correction(db, invitation_id, actor, *, attest_not_sent):
    """Save a new immutable correction version; original exported invitation stays unchanged."""
    if attest_not_sent is not True: raise ValueError('Explicit not-sent attestation is required')
    stamp=now()
    with db.transaction(serialized=True) as c:
        inv=c.execute(select(invitations).where(invitations.c.id==invitation_id)).mappings().first()
        if not inv: raise LookupError('Invitation not found')
        batch=c.execute(select(batches).where(batches.c.id==inv['batch_id'])).mappings().one()
        if inv['send_status']!='NOT_SENT' or batch['send_status']!='NOT_SENT':
            raise ValueError('The batch must be explicitly recorded NOT_SENT before preparing a correction')
        if inv['consent_status']!='NOT_RECEIVED' or c.scalar(select(submissions.c.id).where(submissions.c.invitation_id==invitation_id)) or c.scalar(select(jobs.c.id).where(jobs.c.invitation_id==invitation_id)):
            raise ValueError('Invitation has consent or research activity and cannot be corrected as unsent')
        candidate=c.execute(select(candidates).where(candidates.c.id==inv['candidate_id'])).mappings().one()
        p=parse(candidate['personalization'])
        try: validate_invitation_personalization(c,candidate,p,inv['recipient'])
        except ValueError as exc: raise ValueError('Current personalization does not pass quality validation: '+str(exc)) from exc
        invitation_template=c.execute(select(email_templates).where(email_templates.c.active.is_(True),
            email_templates.c.template_kind=='INVITATION').order_by(email_templates.c.version.desc()).limit(1)).mappings().first()
        subject,body=render(inv['company_name'],inv['website'],p,inv['form_url'],inv['reference'],record=candidate,
            template_config=invitation_template)
        current_version=c.scalar(select(func.max(invitation_corrections.c.version)).where(invitation_corrections.c.invitation_id==invitation_id)) or 1
        source_hash=hashlib.sha256((str(inv['subject'])+'\0'+str(inv['body'])).encode()).hexdigest()
        proposed_hash=hashlib.sha256((subject+'\0'+body).encode()).hexdigest()
        ident='inv-correction-'+uuid.uuid4().hex
        evidence=dump({'personalization':p,'source_urls':p.get('source_urls',[]),'quality_status':candidate['personalization_quality_status'],
            'quality_errors':parse(candidate['personalization_quality_errors'],[]),'source_invitation_id':invitation_id,
            'source_invitation_reference':inv['reference'],'source_invitation_version':inv['template_version']})
        c.execute(invitation_corrections.insert().values(id=ident,invitation_id=invitation_id,version=current_version+1,
            source_sha256=source_hash,proposed_sha256=proposed_hash,subject=subject,body=body,recipient=inv['recipient'],
            evidence=evidence,status='PENDING_REVIEW',created_by=actor,created_at=stamp))
        c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=invitation_id,event='CORRECTION_VERSION_PROPOSED',
            detail=dump({'correction_id':ident,'version':current_version+1,'source_sha256':source_hash,'proposed_sha256':proposed_hash}),created_at=stamp))
        return {'id':ident,'invitation_id':invitation_id,'version':current_version+1,'status':'PENDING_REVIEW',
            'subject':subject,'body':body,'recipient':inv['recipient'],'evidence':evidence,'sha256':proposed_hash}

def approve_invitation_correction(db, correction_id, actor, expected_sha256):
    """Approve and package one exact corrected draft without replacing its source invitation."""
    import io, zipfile
    from .package import build_package
    with db.transaction(serialized=True) as c:
        correction=c.execute(select(invitation_corrections).where(invitation_corrections.c.id==correction_id)).mappings().first()
        if not correction: raise LookupError('Invitation correction not found')
        if correction['status']!='PENDING_REVIEW' or correction['proposed_sha256']!=expected_sha256:
            raise ValueError('Correction is not pending review or its exact content hash does not match')
        inv=c.execute(select(invitations).where(invitations.c.id==correction['invitation_id'])).mappings().one()
        batch=c.execute(select(batches).where(batches.c.id==inv['batch_id'])).mappings().one()
        if inv['send_status']!='NOT_SENT' or batch['send_status']!='NOT_SENT' or inv['consent_status']!='NOT_RECEIVED':
            raise ValueError('Source invitation is no longer confirmed unsent')
        if c.scalar(select(submissions.c.id).where(submissions.c.invitation_id==inv['id'])) or c.scalar(select(jobs.c.id).where(jobs.c.invitation_id==inv['id'])):
            raise ValueError('Source invitation has consent or research activity')
        source_hash=hashlib.sha256((str(inv['subject'])+'\0'+str(inv['body'])).encode()).hexdigest()
        if source_hash!=correction['source_sha256']:
            raise ValueError('Source invitation changed since this correction was prepared')
        corrected_hash=hashlib.sha256((str(correction['subject'])+'\0'+str(correction['body'])).encode()).hexdigest()
        if corrected_hash!=correction['proposed_sha256'] or corrected_hash!=expected_sha256:
            raise ValueError('Correction contents changed since this version was reviewed')
        evidence=parse(correction['evidence']);p=evidence.get('personalization') or {}
        candidate=c.execute(select(candidates).where(candidates.c.id==inv['candidate_id'])).mappings().one()
        validate_invitation_personalization(c,candidate,p,inv['recipient'])
        item={**dict(inv),'subject':correction['subject'],'body':correction['body'],'personalization':dump(p),
            'template_version':(inv['template_version'] or VERSION)+'+quality-correction-v1'}
        package=build_package([item],[],BULK_SUBJECT)
        package_buffer=io.BytesIO(package)
        with zipfile.ZipFile(package_buffer,'a',zipfile.ZIP_DEFLATED) as z:
            z.writestr('correction-version.json',dump({'correction_id':correction_id,'invitation_id':inv['id'],
                'invitation_reference':inv['reference'],'version':correction['version'],'source_sha256':source_hash,
                'corrected_sha256':correction['proposed_sha256'],'status':'APPROVED_VERSIONED_COPY'}))
        package=package_buffer.getvalue()
        c.execute(invitation_corrections.update().where(invitation_corrections.c.id==correction_id).values(
            status='APPROVED',approved_by=actor,approved_at=now(),package=package))
        c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=inv['id'],event='CORRECTION_VERSION_APPROVED',
            detail=dump({'correction_id':correction_id,'version':correction['version'],'sha256':correction['proposed_sha256'],'reviewer':actor}),created_at=now()))
        return {'id':correction_id,'version':correction['version'],'package':package,'sha256':correction['proposed_sha256']}
