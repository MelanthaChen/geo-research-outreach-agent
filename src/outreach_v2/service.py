from __future__ import annotations
import hashlib,json,secrets,uuid
from sqlalchemy import select,func,and_,or_,exists
from .db import candidates,contacts,invitations,batches,jobs,submissions,reports,deliveries,suppressions,events,worker_runs,now
from .template import validate,render,VERSION,BULK_SUBJECT,EMAIL,same_site
from .migrate import dump,parse,sha,unpack
from outreach_agent.google_forms_integration import prefilled_form_url

FILTERS={'kind','industry','location','website_status','contact','eligibility','personalization_status','invitation_status','consent_status','research_status','q'}

def email_contacts():
    # This is a prefilter, not the final email/evidence validation at generation.
    first_party=or_(*[contacts.c.source_url.like(prefix+candidates.c.domain+'/%') for prefix in ('https://','http://','https://www.','http://www.')],
                    *[contacts.c.source_url==prefix+candidates.c.domain for prefix in ('https://','http://','https://www.','http://www.')])
    return and_(contacts.c.candidate_id==candidates.c.id,contacts.c.email.like('%@%.%'),contacts.c.suppressed.is_(False),
                contacts.c.verification.not_in(['REJECTED','SUPPRESSED','INVALID']),
                or_(first_party, and_(candidates.c.fixture.is_(True),contacts.c.verification=='OWNER_AUTHORIZED')),
                ~exists(select(suppressions.c.email).where(suppressions.c.email==contacts.c.email)))

def readiness():
    return and_(candidates.c.website_status=='VERIFIED',candidates.c.qualification.in_(['ELIGIBLE','QUALIFIED']),
        candidates.c.suppressed.is_(False),candidates.c.personalization_status=='READY',
        select(func.count(func.distinct(contacts.c.email))).where(email_contacts()).correlate(candidates).scalar_subquery()==1,
        ~exists(select(invitations.c.id).where(invitations.c.candidate_id==candidates.c.id)))

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
        elif key in {'kind','website_status','personalization_status'}:clauses.append(getattr(candidates.c,key)==value)
        elif key=='contact':
            condition=exists(select(contacts.c.id).where(contacts.c.candidate_id==candidates.c.id,
                contacts.c.email!='' if value=='email' else contacts.c.url!=''))
            clauses.append(~condition if value=='none' else condition)
        elif key=='eligibility':clauses.append(readiness() if value=='eligible' else ~readiness())
        elif key=='invitation_status':
            condition=exists(select(invitations.c.id).where(invitations.c.candidate_id==candidates.c.id))
            if value=='NOT_GENERATED':clauses.append(~condition)
            elif value=='EXPORTED':clauses.append(exists(select(invitations.c.id).where(invitations.c.candidate_id==candidates.c.id,invitations.c.exported_at.is_not(None))))
            else:clauses.append(exists(select(invitations.c.id).where(invitations.c.candidate_id==candidates.c.id,invitations.c.send_status==value)))
        elif key=='consent_status':clauses.append(exists(select(invitations.c.id).where(invitations.c.candidate_id==candidates.c.id,invitations.c.consent_status==value)))
        elif key=='research_status':clauses.append(exists(select(jobs.c.id).join(invitations).where(invitations.c.candidate_id==candidates.c.id,jobs.c.status==value)))
    return clauses

def list_candidates(db,filters=None,page=1,page_size=50):
    filters=filters or {};page=max(1,page);page_size=max(1,min(page_size,100));clauses=predicates(filters)
    with db.transaction() as c:
        total=c.scalar(select(func.count()).select_from(candidates).where(*clauses))
        eligible=c.scalar(select(func.count()).select_from(candidates).where(*clauses,readiness()))
        query=select(candidates,readiness().label('eligible')).where(*clauses).order_by(candidates.c.name,candidates.c.id).limit(page_size).offset((page-1)*page_size)
        rows=[dict(r) for r in c.execute(query).mappings()]
        for row in rows:
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
        return {'rows':rows,'total':total,'eligible_matching':eligible,'page':page,'page_size':page_size}

def detail(db,ident):
    with db.transaction() as c:
        row=c.execute(select(candidates).where(candidates.c.id==ident)).mappings().first()
        if not row:raise LookupError('Candidate not found')
        row=dict(row);row['personalization']=parse(row['personalization'])
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
    if row['suppressed']:reasons.append('Candidate suppressed or withdrawn')
    if row['website_status']!='VERIFIED':reasons.append('Official website not verified')
    if row['qualification'] not in {'ELIGIBLE','QUALIFIED'}:reasons.append('Candidate not qualified')
    if c.scalar(select(invitations.c.id).where(invitations.c.candidate_id==row['id'])):reasons.append('Invitation already exists; download its saved batch')
    try:validate(parse(row['personalization']),row['website'])
    except ValueError as e:reasons.append(str(e))
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
    return (next(iter(emails)) if len(emails)==1 else None),reasons

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
            query=select(candidates).where(*predicates(selection.get('filters') or {}),readiness()).order_by(candidates.c.id)
        else:
            ids=selection.get('ids') or []
            if not ids:raise ValueError('Select at least one company')
            if len(ids)>100000:raise ValueError('Selection too large')
            query=select(candidates).where(candidates.c.id.in_(ids)).order_by(candidates.c.id)
        batch_id='batch-'+uuid.uuid4().hex;generated=[];excluded=[];seen=set()
        c.execute(batches.insert().values(id=batch_id,request_key=key,selection_hash=selection_hash,created_at=stamp,bulk_subject=BULK_SUBJECT))
        for row in c.execute(query).mappings():
            recipient,reasons=eligible_recipient(c,row)
            if recipient in seen:reasons.append('Duplicate recipient within batch')
            if recipient and c.scalar(select(invitations.c.id).where(invitations.c.recipient==recipient)):reasons.append('Recipient already has an invitation; resend is not automatic')
            if reasons:
                excluded.append({'candidate_id':row['id'],'company':row['name'],'reasons':reasons});continue
            p=parse(row['personalization'])
            invitation_id='inv-'+uuid.uuid4().hex;reference='GEO-'+secrets.token_hex(16).upper();url=prefilled_form_url(reference)
            try:
                subject,body=render(row['name'],row['website'],p,url,reference,row['fixture'])
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
                      template_version=VERSION,personalization=dump(p),generated_at=stamp,exported_at=stamp,
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
                'eligible':c.scalar(select(func.count()).select_from(candidates).where(readiness())),
                'personalization_ready':c.scalar(select(func.count()).select_from(candidates).where(candidates.c.personalization_status=='READY')),
                'invitations_exported':c.scalar(select(func.count()).select_from(invitations).where(invitations.c.exported_at.is_not(None))),
                'form_responses':c.scalar(select(func.count()).select_from(submissions)),
                'authorized_jobs':c.scalar(select(func.count()).select_from(jobs)),
                'research_completed':c.scalar(select(func.count()).select_from(jobs).where(jobs.c.completed_at.is_not(None))),
                'reports_accepted':c.scalar(select(func.count()).select_from(deliveries).where(deliveries.c.status=='ACCEPTED')),
                'failures':c.scalar(select(func.count()).select_from(jobs).where(jobs.c.error.is_not(None))),
                'latest_worker_run':({'id':run['id'],'status':run['status'],'started_at':run['started_at'],
                    'detail':parse(run['detail'])} if run else None)}