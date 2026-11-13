from __future__ import annotations
import hashlib,json,secrets,uuid
from sqlalchemy import select,func,and_,or_,exists,case
from .db import candidates,contacts,invitations,batches,jobs,submissions,reports,deliveries,suppressions,events,worker_runs,questionnaires,invitation_corrections,email_templates,now
from .template import validate,render,VERSION,BULK_SUBJECT,EMAIL,same_site
from .migrate import dump,parse,sha,unpack
from .owner_test_fixtures import OWNER_TEST_FIXTURES, SYNTHETIC_CLASSIFICATION, synthetic_fixture_copy
from outreach_agent.google_forms_integration import prefilled_form_url

FILTERS={'kind','industry','location','contact','primary_status','completed_personalization','q'}

def email_contacts():
    # This is a prefilter, not the final email/evidence validation at generation.
    first_party=or_(*[contacts.c.source_url.like(prefix+candidates.c.domain+'/%') for prefix in ('https://','http://','https://www.','http://www.')],
                    *[contacts.c.source_url==prefix+candidates.c.domain for prefix in ('https://','http://','https://www.','http://www.')])
    purpose=func.coalesce(contacts.c.purpose,'')
    personal_or_restricted=or_(*[purpose.ilike('%'+term+'%') for term in ('privacy','career','recruit','personal','legal','abuse')])
    return and_(contacts.c.candidate_id==candidates.c.id,contacts.c.email.like('%@%.%'),contacts.c.email.not_like('% %'),contacts.c.suppressed.is_(False),
                or_(contacts.c.verification.is_(None),contacts.c.verification.not_in(['REJECTED','SUPPRESSED','INVALID'])),
                or_(first_party, and_(candidates.c.fixture.is_(True),contacts.c.verification=='OWNER_AUTHORIZED')),
                ~personal_or_restricted,
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
        (contact_count>1,'BLOCKED'),
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

def owner_test_recipient_authorized(c, row, recipient):
    """Recheck the exact fixture's saved owner binding at every invitation gate."""
    fixture=OWNER_TEST_FIXTURES.get(row.get('id'))
    if (not fixture or row.get('fixture') is not True or row.get('website')!=fixture['website']
        or row.get('website_status')!='VERIFIED' or row.get('qualification') not in {'ELIGIBLE','QUALIFIED'}
        or row.get('suppressed') or (recipient or '').strip().casefold()!=fixture['recipient'].casefold()):
        return False
    saved=c.execute(select(contacts).where(contacts.c.candidate_id==row['id'])).mappings().all()
    if len(saved)!=1:
        return False
    contact=saved[0]
    if ((contact['email'] or '').strip().casefold()!=fixture['recipient'].casefold()
        or contact['verification']!='OWNER_AUTHORIZED' or contact['suppressed']
        or not same_site(row['website'],contact['source_url'])):
        return False
    return not bool(c.scalar(select(suppressions.c.email).where(
        suppressions.c.email==fixture['recipient'].casefold())))

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

def list_candidates(db,filters=None,page=1,page_size=50):
    filters=filters or {};page=max(1,page);page_size=max(1,min(page_size,100));clauses=predicates(filters)
    with db.transaction() as c:
        total=c.scalar(select(func.count()).select_from(candidates).where(*clauses))
        status=primary_status_expression()
        eligible=c.scalar(select(func.count()).select_from(candidates).where(*clauses,status=='READY_TO_INVITE'))
        completed_count=c.scalar(select(func.count()).select_from(candidates).where(completed_personalization()))
        status_counts={key:c.scalar(select(func.count()).select_from(candidates).where(status==key)) or 0 for key in STATUS_LABELS}
        query=select(candidates,status.label('primary_status')).where(*clauses).order_by(candidates.c.name,candidates.c.id).limit(page_size).offset((page-1)*page_size)
        rows=[dict(r) for r in c.execute(query).mappings()]
        for row in rows:
            row['status_label']=STATUS_LABELS[row['primary_status']]
            row['status_reason']=status_reason(row,row['primary_status'])
            row['eligible']=row['primary_status']=='READY_TO_INVITE'
            row.pop('personalization',None)
            row['contacts']=[dict(x) for x in c.execute(select(contacts.c.email,contacts.c.url,contacts.c.verification).where(contacts.c.candidate_id==row['id'])).mappings()]
            row['invitation']=c.execute(select(invitations.c.id,invitations.c.send_status,invitations.c.consent_status,invitations.c.exported_at).where(invitations.c.candidate_id==row['id'])).mappings().first()
            if row['invitation']:
                row['invitation']=dict(row['invitation'])
                row['research']=c.execute(select(jobs.c.status,jobs.c.error).where(jobs.c.invitation_id==row['invitation']['id'])).mappings().first()
                row['research']=dict(row['research']) if row['research'] else None
                # Spell out the delivery -> report -> job path; implicit joins are ambiguous here.
                row['delivery']=c.scalar(
                    select(deliveries.c.status)
                    .select_from(deliveries)
                    .join(reports, deliveries.c.report_id==reports.c.id)
                    .join(jobs, reports.c.job_id==jobs.c.id)
                    .where(jobs.c.invitation_id==row['invitation']['id'])
                )
        return {'rows':rows,'total':total,'eligible_matching':eligible,'completed_personalization_count':completed_count,
            'status_counts':status_counts,'page':page,'page_size':page_size}

def detail(db,ident):
    with db.transaction() as c:
        row=c.execute(select(candidates).where(candidates.c.id==ident)).mappings().first()
        if not row:raise LookupError('Candidate not found')
        row=dict(row);row['personalization']=parse(row['personalization'])
        # The exact same derived status expression powers the table, filters, and detail view.
        row['primary_status']=c.scalar(select(primary_status_expression()).where(candidates.c.id==ident))
        row['status_label']=STATUS_LABELS[row['primary_status']]
        row['status_reason']=status_reason(row,row['primary_status'])
        row['invitation']=c.execute(select(invitations.c.id,invitations.c.send_status,invitations.c.consent_status,
            invitations.c.exported_at).where(invitations.c.candidate_id==ident)).mappings().first()
        row['invitation']=dict(row['invitation']) if row['invitation'] else None
        row['contacts']=[dict(r) for r in c.execute(select(contacts).where(contacts.c.candidate_id==ident)).mappings()]
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

def eligible_recipient(c,row):
    reasons=[]
    personalization=parse(row['personalization'])
    synthetic_fixture=synthetic_fixture_copy(row,personalization)
    if not synthetic_fixture and (row.get('personalization_analysis_version')!='semantic-openai-v1' or int(row.get('personalization_version') or 0)<1):
        reasons.append('Personalization is legacy, missing, or outdated; current semantic version required')
    if row['suppressed']:reasons.append('Candidate suppressed or withdrawn')
    if row['website_status']!='VERIFIED':reasons.append('Official website not verified')
    if row['qualification'] not in {'ELIGIBLE','QUALIFIED'}:reasons.append('Candidate not qualified')
    if c.scalar(select(invitations.c.id).where(invitations.c.candidate_id==row['id'])):reasons.append('Invitation already exists; download its saved batch')
    try:validate(personalization,row['website'],record=row)
    except ValueError as e:reasons.append(str(e))
    if not synthetic_fixture and row.get('personalization_quality_status')!='PASS':
        reasons.append('Personalization quality is '+str(row.get('personalization_quality_status') or 'UNVERIFIED')+
                       (': '+str(row.get('personalization_quality_errors') or 'quality audit required') if row.get('personalization_quality_errors') else ''))
    emails=set()
    for contact in c.execute(select(contacts).where(contacts.c.candidate_id==row['id'])).mappings():
        email=(contact['email'] or '').lower()
        if not EMAIL.fullmatch(email) or contact['suppressed'] or contact['verification'] in {'REJECTED','SUPPRESSED','INVALID'}:continue
        if c.scalar(select(suppressions.c.email).where(suppressions.c.email==email)):continue
        if not same_site(row['website'],contact['source_url']) and not (row['fixture'] and contact['verification']=='OWNER_AUTHORIZED'):continue
        # A source explicitly labeled personal/hiring/privacy is not a general business recipient.
        if any(x in (contact['purpose'] or '').lower() for x in ('privacy','career','recruit','personal','legal','abuse')):continue
        emails.add(email)
    if len(emails)!=1:reasons.append('No single valid first-party business email' if not emails else 'Multiple recipient emails; no unambiguous recipient')
    if synthetic_fixture:
        expected=OWNER_TEST_FIXTURES[row['id']]['recipient']
        if len(emails)==1 and next(iter(emails))!=expected:
            reasons.append('Synthetic test copy is restricted to its exact owner-authorized recipient')
        elif len(emails)==1 and not owner_test_recipient_authorized(c,row,next(iter(emails))):
            reasons.append('Synthetic test copy requires its exact unsuppressed OWNER_AUTHORIZED fixture recipient')
    return (next(iter(emails)) if len(emails)==1 else None),reasons

def select_pilot_cohort(c, limit=100):
    """Select a fixed research-preparation cohort before personalization.

    A cohort member is a verified, eligible company with one independently
    first-party, verified business email and preserved source provenance. This
    deliberately does not require personalization or create invitations.
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
            if (not EMAIL.fullmatch(email) or contact['suppressed'] or contact['verification']!='VERIFIED'
                or not same_site(row['website'],contact['source_url']) or not (contact['evidence'] or '').strip()
                or c.scalar(select(suppressions.c.email).where(suppressions.c.email==email))):
                continue
            if any(term in (contact['purpose'] or '').casefold() for term in ('privacy','career','recruit','personal','legal','abuse')):
                continue
            matching.append(dict(contact))
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

def generate(db,selection,key):
    if not isinstance(key,str) or not 16<=len(key)<=128:raise ValueError('A stable request key is required')
    if set(selection)-{'ids','filters','all_matching'}:raise ValueError('Invalid selection')
    selection_hash=sha(dump(selection));stamp=now()
    from .package import build_package
    from .template import host
    with db.transaction(serialized=True) as c:
        prior=c.execute(select(batches).where(batches.c.request_key==key)).mappings().first()
        if prior:
            if prior['selection_hash']!=selection_hash:raise ValueError('Request key belongs to a different selection')
            return dict(prior)
        if selection.get('all_matching'):
            query=select(candidates).where(*predicates(selection.get('filters') or {}),primary_status_expression()=='READY_TO_INVITE').order_by(candidates.c.id)
        else:
            ids=selection.get('ids') or []
            if not ids:raise ValueError('Select at least one company')
            if len(ids)>100000:raise ValueError('Selection too large')
            query=select(candidates).where(candidates.c.id.in_(ids)).order_by(candidates.c.id)
        batch_id='batch-'+uuid.uuid4().hex;generated=[];excluded=[];seen=set()
        questionnaire=c.execute(select(questionnaires).where(questionnaires.c.active.is_(True)).order_by(questionnaires.c.version.desc()).limit(1)).mappings().first()
        invitation_template=c.execute(select(email_templates).where(email_templates.c.active.is_(True),
            email_templates.c.template_kind=='INVITATION').order_by(email_templates.c.version.desc()).limit(1)).mappings().first()
        questionnaire_fields=parse(questionnaire['field_map']) if questionnaire else {}
        configured_form=questionnaire['form_url'] if questionnaire else None
        reference_entry=questionnaire_fields.get('invitation_reference_entry_id') if questionnaire else None
        c.execute(batches.insert().values(id=batch_id,request_key=key,selection_hash=selection_hash,created_at=stamp,
            bulk_subject=BULK_SUBJECT,pilot_review_status='PENDING_REVIEW'))
        for row in c.execute(query).mappings():
            recipient,reasons=eligible_recipient(c,row)
            if recipient in seen:reasons.append('Duplicate recipient within batch')
            if recipient and c.scalar(select(invitations.c.id).where(invitations.c.recipient==recipient)):reasons.append('Recipient already has an invitation; resend is not automatic')
            if reasons:
                excluded.append({'candidate_id':row['id'],'company':row['name'],'reasons':reasons});continue
            p=parse(row['personalization'])
            invitation_id='inv-'+uuid.uuid4().hex;reference='GEO-'+secrets.token_hex(16).upper()
            url=prefilled_form_url(reference,form_url=configured_form,entry_id=reference_entry)
            try:
                subject,body=render(row['name'],row['website'],p,url,reference,record=row,
                    template_config=invitation_template)
            except ValueError as exc:
                excluded.append({'candidate_id':row['id'],'company':row['name'],
                                 'reasons':['Invalid invitation personalization: '+str(exc)]})
                continue
            authority={'method':'INVITATION_EMAIL_AND_FIRST_PARTY_DOMAIN','website':row['website']}
            if row['fixture']:
                binding=OWNER_BINDINGS.get(host(row['website']))
                if not binding or binding['recipient']!=recipient:
                    excluded.append({'candidate_id':row['id'],'company':row['name'],'reasons':['Owner recipient binding mismatch']});continue
                authority={'method':'OWNER_BINDING','website':row['website'],**binding}
            item=dict(id=invitation_id,candidate_id=row['id'],batch_id=batch_id,reference=reference,recipient=recipient,
                      website=row['website'],company_name=row['name'],subject=subject,body=body,form_url=url,
                      template_version=(f"invitation-template-v{invitation_template['version']}" if invitation_template else VERSION),
                      questionnaire_id=questionnaire['id'] if questionnaire else None,
                      researcher_name=(invitation_template['researcher_name'] if invitation_template else __import__('os').getenv('OUTREACH_RESEARCHER_NAME')),
                      university_affiliation=(invitation_template['university_affiliation'] if invitation_template else __import__('os').getenv('OUTREACH_UNIVERSITY_AFFILIATION')),
                      reply_to=(invitation_template['reply_to'] if invitation_template else __import__('os').getenv('OUTREACH_REPLY_TO')),
                      sender_name=(invitation_template.get('sender_name') if invitation_template else (__import__('os').getenv('OUTREACH_INVITATION_SENDER_NAME') or __import__('os').getenv('OUTREACH_RESEARCHER_NAME'))),
                      sender_email=(invitation_template['sender'] if invitation_template else (__import__('os').getenv('OUTREACH_INVITATION_SENDER') or __import__('os').getenv('OUTREACH_REPLY_TO'))),
                      personalization=dump(p),generated_at=stamp,exported_at=stamp,
                      send_status='UNVERIFIED',consent_status='NOT_RECEIVED',authority=dump(authority))
            c.execute(invitations.insert().values(**item));generated.append(item);seen.add(recipient)
        if not generated:
            raise ValueError('No eligible invitations: '+dump(excluded))
        package=build_package(generated,excluded,BULK_SUBJECT)
        c.execute(batches.update().where(batches.c.id==batch_id).values(package=package,excluded=dump(excluded),exported_at=stamp))
        c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=batch_id,event='EXPORTED',detail=dump({'count':len(generated),'excluded':len(excluded)}),created_at=stamp))
        return dict(c.execute(select(batches).where(batches.c.id==batch_id)).mappings().one())

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
