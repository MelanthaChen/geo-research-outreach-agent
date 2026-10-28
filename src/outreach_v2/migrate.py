"""Resumable, source-preserving import. No source database is ever writable.

All candidate/evidence rows are archived with their full JSON, hash and identity
mapping. Historical workflow relations are inventoried and isolated, not replayed.
Source priority is explicit: primary stores, cloud registry, local registry, then
historical snapshots and exports. Older snapshots cannot overwrite current data.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, sqlite3, zlib, base64
from pathlib import Path
from sqlalchemy import select, func
from .db import Database, candidates, contacts, sources, source_rows, source_memberships, suppressions, now
from .template import host, validate

WORKFLOW = {'questionnaire_invitations','questionnaire_submissions','email_drafts','email_events','replies',
 'geo_handoffs','dispatch_attempts','consent_reviews','questionnaire_conflicts','geo_handoff_attempts',
 'confirmation_emails','research_reports','invitation_batches','owner_test_recipient_bindings',
 'campaigns','campaign_items','demo_interest_submissions','campaign_audit','demo_control','outreach','responses',
 'mail_merge_reconciliations','admin_sessions','admin_auth_events','admin_users'}
WORKFLOW_EXPORTS={'phase5a_delivery_simulation.csv','phase5a_email_drafts.csv','phase5a_contact_form_drafts.csv','phase5d_demo_campaign_summary.csv','phase5d_demo_outreach_status.csv'}
FIXTURE_IDS={'geoairesume-owner-test-v1','whisker-haven-owner-demo-20261023-v1'}

def dump(x): return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'),default=str)
def sha(x): return hashlib.sha256(x.encode()).hexdigest()
def postgres_text(value):return value.replace('\x00','\ufffd') if isinstance(value,str) else value
def parse(x,default=None):
    if not x: return {} if default is None else default
    if isinstance(x,(dict,list)): return x
    try: return json.loads(x)
    except (ValueError,TypeError): return {} if default is None else default

def pack(raw):return 'zlib:'+base64.b64encode(zlib.compress(raw.encode(),6)).decode()
def unpack(raw):return zlib.decompress(base64.b64decode(raw[5:])).decode() if raw.startswith('zlib:') else raw

def readonly(path):
    c=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True);c.row_factory=sqlite3.Row
    return c

def fixture(row):
    return row.get('profile_type')=='owner_controlled_test' or row.get('profile_id') in FIXTURE_IDS or row.get('data_classification')=='DEMO_SYNTHETIC' or host(row.get('website') or row.get('official_website')) in {'geo-ai-resume.vercel.app','whisker-haven-demo.vercel.app'} or str(row.get('company_name') or '').startswith('Synthetic Test Company')

class Migrator:
    def __init__(self, db, batch_size=500):
        self.db=db;self.batch_size=batch_size
        with db.transaction() as c:
            identities=c.execute(select(candidates.c.id,candidates.c.kind,candidates.c.domain,candidates.c.name)).all()
        # Bounded identity index only; no source datasets or page bodies materialized.
        self.ids={r.id for r in identities}
        self.domains={}
        for r in identities:
            if r.kind=='company' and r.domain: self.domains.setdefault(r.domain,[]).append((r.id,r.name.casefold()))
        self.local_map={}

    def lean_operational_snapshot(self,path,*,archive_manifest,batch_size=500,expected=None):
        """Copy current operational rows only; full source payloads stay in the verified archive."""
        archive_manifest=Path(archive_manifest)
        manifest=json.loads(archive_manifest.read_text())
        archive=Path(manifest['source_snapshot'])
        if not archive.is_file() or archive.stat().st_size!=manifest.get('bytes'):
            raise ValueError('Verified source archive is missing or has a different size')
        def file_sha(target):
            h=hashlib.sha256()
            with target.open('rb') as f:
                for chunk in iter(lambda:f.read(8*1024*1024),b''):h.update(chunk)
            return h.hexdigest()
        archived_sha=manifest.get('sha256')
        if not archived_sha or file_sha(archive)!=archived_sha or file_sha(Path(path))!=archived_sha:
            raise ValueError('Source snapshot does not match the verified archive checksum')
        if not 1<=batch_size<=5000:raise ValueError('Lean import batch size must be 1..5000')
        with readonly(path) as source:
            tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required={'candidates','contacts','sources','source_rows','source_memberships'}
            if not required.issubset(tables):raise ValueError('Input is not the reconciled V2 snapshot')
            counts={r[0]:r[1] for r in source.execute('SELECT kind,COUNT(*) FROM candidates GROUP BY kind')}
            contact_count=source.execute('SELECT COUNT(*) FROM contacts').fetchone()[0]
            fixture_count=source.execute('SELECT COUNT(*) FROM candidates WHERE fixture!=0').fetchone()[0]
            isolated_count=source.execute("SELECT COUNT(*) FROM source_rows WHERE disposition='ISOLATED'").fetchone()[0]
            memberships=source.execute('SELECT COALESCE(SUM(count),0) FROM source_memberships').fetchone()[0]
            incomplete_sources=source.execute("SELECT COUNT(*) FROM sources WHERE status!='COMPLETE' OR expected!=processed").fetchone()[0]
            unmapped_contacts=source.execute('''SELECT COUNT(*) FROM contacts x LEFT JOIN candidates c ON c.id=x.candidate_id WHERE c.id IS NULL''').fetchone()[0]
            if fixture_count:raise ValueError('Owner fixtures must not enter the real-candidate namespace')
            if incomplete_sources or unmapped_contacts:raise ValueError('Snapshot source reconciliation or contact identity mapping is incomplete')
            if expected:
                if counts!=expected.get('candidates') or contact_count!=expected.get('contacts') or isolated_count!=expected.get('isolated_rows') or memberships!=expected.get('source_memberships'):
                    raise ValueError('Snapshot counts differ from the approved migration reconciliation')
            existing_candidates=0;existing_contacts=0;existing_rows=0;existing_memberships=0
            with self.db.transaction() as c:
                existing_candidates=c.execute(select(func.count()).select_from(candidates)).scalar_one()
                existing_contacts=c.execute(select(func.count()).select_from(contacts)).scalar_one()
                existing_rows=c.execute(select(func.count()).select_from(source_rows)).scalar_one()
                existing_memberships=c.execute(select(func.count()).select_from(source_memberships)).scalar_one()
            if existing_rows or existing_memberships:
                raise ValueError('Lean V2 namespace already contains duplicated archival payloads; refusing to mix import modes')
            if existing_candidates>sum(counts.values()) or existing_contacts>contact_count:
                raise ValueError('V2 operational namespace contains rows outside this snapshot')

            nul_replacements=0
            def storage_row(item):
                nonlocal nul_replacements
                if self.db.postgres:
                    for key,value in item.items():
                        if isinstance(value,str) and '\x00' in value:
                            nul_replacements+=value.count('\x00');item[key]=postgres_text(value)
                return item

            # Map canonical candidates to their immutable content-addressed row in the archive.
            candidate_refs={}
            ref_query='''SELECT c.id,
                (SELECT sr.id FROM source_rows sr JOIN sources s ON s.id=sr.source_id
                 WHERE sr.candidate_id=c.id AND sr.disposition='PRESERVED' AND s.path=c.source
                 ORDER BY CASE WHEN s.relation IN ('candidates','companies','research_records','csv') THEN 0 ELSE 1 END,
                          s.relation,sr.source_key LIMIT 1),
                (SELECT sr.checksum FROM source_rows sr JOIN sources s ON s.id=sr.source_id
                 WHERE sr.candidate_id=c.id AND sr.disposition='PRESERVED' AND s.path=c.source
                 ORDER BY CASE WHEN s.relation IN ('candidates','companies','research_records','csv') THEN 0 ELSE 1 END,
                          s.relation,sr.source_key LIMIT 1)
                FROM candidates c ORDER BY c.id'''
            for ident,ref,checksum in source.execute(ref_query):
                if ref:candidate_refs[ident]=(ref,checksum)
            contact_checksums={}
            for ident,candidate_id,raw in source.execute('SELECT id,candidate_id,raw FROM contacts ORDER BY id'):
                checksum=sha(raw or '')
                contact_checksums[(candidate_id,checksum)]=(ident,checksum)
            direct_contact_refs={}
            contact_ref_query='''SELECT sr.id,sr.candidate_id,sr.checksum FROM source_rows sr
                JOIN sources s ON s.id=sr.source_id
                WHERE sr.disposition='PRESERVED' AND sr.candidate_id IS NOT NULL
                  AND (lower(s.relation) LIKE '%contact%' OR lower(s.relation) LIKE '%channel%')'''
            for ref,candidate_id,checksum in source.execute(contact_ref_query):
                key=(candidate_id,checksum)
                if key in contact_checksums:direct_contact_refs[key]=(ref,checksum)

            source_rows_data=[dict(r) for r in source.execute('SELECT * FROM sources ORDER BY id')]
            for offset in range(0,len(source_rows_data),batch_size):
                batch=[storage_row(item) for item in source_rows_data[offset:offset+batch_size]]
                with self.db.transaction() as c:self.db.upsert(c,sources,batch,update=False)
            total=0;batch=[]
            for raw in source.execute('SELECT * FROM candidates ORDER BY id'):
                item=dict(raw);reference=candidate_refs.get(item['id'])
                if reference:
                    item['source_ref']='source_row:'+reference[0];item['source_checksum']=reference[1]
                else:
                    item['source_ref']='snapshot_candidate:'+item['id'];item['source_checksum']=sha(dump(item))
                for key in ('suppressed','fixture'):item[key]=bool(item[key])
                storage_row(item)
                batch.append(item);total+=1
                if len(batch)>=batch_size:
                    with self.db.transaction() as c:self.db.upsert(c,candidates,batch,update=False)
                    batch=[]
            if batch:
                with self.db.transaction() as c:self.db.upsert(c,candidates,batch,update=False)
            total_contacts=0;batch=[]
            for raw in source.execute('SELECT * FROM contacts ORDER BY id'):
                item=dict(raw);key=(item['candidate_id'],sha(item['raw'] or ''));snapshot_checksum=sha(dump(item))
                ref=direct_contact_refs.get(key) or candidate_refs.get(item['candidate_id'])
                if ref:
                    item['source_ref']='source_row:'+ref[0];item['source_checksum']=ref[1]
                else:
                    item['source_ref']='snapshot_contact:'+item['id'];item['source_checksum']=snapshot_checksum
                item['raw']=None
                item['suppressed']=bool(item['suppressed'])
                storage_row(item)
                batch.append(item);total_contacts+=1
                if len(batch)>=batch_size:
                    with self.db.transaction() as c:self.db.upsert(c,contacts,batch,update=False)
                    batch=[]
            if batch:
                with self.db.transaction() as c:self.db.upsert(c,contacts,batch,update=False)
        with self.db.transaction() as c:
            persisted={r.kind:r.n for r in c.execute(select(candidates.c.kind,func.count().label('n')).group_by(candidates.c.kind))}
            persisted_contacts=c.scalar(select(func.count()).select_from(contacts))
            raw_contacts=c.scalar(select(func.count()).select_from(contacts).where(contacts.c.raw.is_not(None)))
            missing_candidate_refs=c.scalar(select(func.count()).select_from(candidates).where(candidates.c.source_ref.is_(None)))
            missing_contact_refs=c.scalar(select(func.count()).select_from(contacts).where(contacts.c.source_ref.is_(None)))
            candidate_source_row_refs=c.scalar(select(func.count()).select_from(candidates).where(candidates.c.source_ref.like('source_row:%')))
            contact_source_row_refs=c.scalar(select(func.count()).select_from(contacts).where(contacts.c.source_ref.like('source_row:%')))
            candidate_snapshot_refs=c.scalar(select(func.count()).select_from(candidates).where(candidates.c.source_ref.like('snapshot_candidate:%')))
            contact_snapshot_refs=c.scalar(select(func.count()).select_from(contacts).where(contacts.c.source_ref.like('snapshot_contact:%')))
            v2_archive_rows=c.scalar(select(func.count()).select_from(source_rows))
            v2_memberships=c.scalar(select(func.count()).select_from(source_memberships))
        if persisted!=counts or persisted_contacts!=contact_count or raw_contacts or v2_archive_rows or v2_memberships:
            raise ValueError('Lean operational migration failed persisted count or archive-isolation reconciliation')
        return {'candidates':counts,'contacts':contact_count,'source_catalog_rows':len(source_rows_data),
            'source_archive_rows_preserved_outside_v2':source.execute('SELECT COUNT(*) FROM source_rows').fetchone()[0],
            'source_memberships_preserved_outside_v2':memberships,'isolated_historical_rows_excluded':isolated_count,
            'candidate_rows_missing_archive_ref':missing_candidate_refs,'contact_rows_missing_archive_ref':missing_contact_refs,
            'candidate_source_row_refs':candidate_source_row_refs,'candidate_snapshot_row_refs':candidate_snapshot_refs,
            'contact_source_row_refs':contact_source_row_refs,'contact_snapshot_row_refs':contact_snapshot_refs,
            'v2_source_rows':v2_archive_rows,'v2_source_memberships':v2_memberships,'duplicated_contact_raw_payloads':raw_contacts,
            'postgres_nul_replacements':nul_replacements,'checksum':archived_sha,'archive_path':str(archive),'persisted':True}

    def identity(self,row,kind,path):
        original=str(row.get('candidate_id') or row.get('source_candidate_id') or row.get('profile_id') or '')
        if original in self.ids: return original,False
        name=str(row.get('company_name') or row.get('name') or '').strip()
        website=row.get('website') or row.get('official_website') or ''
        domain=host(website)
        # Only exact company name+domain matches may reconcile older numeric IDs.
        # A hotel's physical-property source ID is never replaced by its chain domain.
        if kind=='company' and domain:
            matches=[ident for ident,n in self.domains.get(domain,[]) if n==name.casefold()]
            if len(matches)==1: return matches[0],False
        ident=original or 'legacy-'+sha(str(path)+':'+str(row.get('id') or name+website))[:24]
        return ident,ident not in self.ids

    def candidate(self,c,row,kind,path,priority=False):
        if fixture(row): return None
        ident,new=self.identity(row,kind,path)
        stamp=now();p=parse(row.get('personalization_json'))
        website=str(row.get('website') or row.get('official_website') or '')
        status='INCOMPLETE';error=None
        if p:
            try: validate(p,website);status='READY'
            except ValueError as e:error=str(e)
        data={'id':ident,'kind':kind,'name':str(row.get('company_name') or row.get('name') or 'Unknown'),
              'website':website,'domain':host(website),'industry':row.get('industry'),'location':row.get('location'),
              'description':p.get('business_description') or row.get('description'),
              'website_status':row.get('identity_verification_status') or row.get('verification_status') or 'UNKNOWN',
              'qualification':row.get('qualification_status') or row.get('geo_eligibility_status') or row.get('eligibility') or 'UNKNOWN',
              'geography':row.get('geographic_classification') or 'UNKNOWN',
              'suppressed':row.get('suppression_status')=='SUPPRESSED' or row.get('withdrawal_status')=='WITHDRAWN',
              'source':str(path),'personalization':dump(p),'personalization_status':status,
              'personalization_at':row.get('personalization_completed_at'),'personalization_error':error,'fixture':False,
              'created_at':row.get('created_at') or stamp,'updated_at':stamp}
        if new:
            self.db.upsert(c,candidates,[data],update=False);self.ids.add(ident)
            if kind=='company' and data['domain']: self.domains.setdefault(data['domain'],[]).append((ident,data['name'].casefold()))
        elif priority:
            # Registry adds suppression, geography, personalization. It never silently reassigns websites.
            existing=c.execute(select(candidates).where(candidates.c.id==ident)).mappings().one()
            changes={'suppressed':bool(existing['suppressed'] or data['suppressed'])}
            if data['geography']!='UNKNOWN' and existing['geography']=='UNKNOWN':changes['geography']=data['geography']
            if status=='READY' and existing['personalization_status']!='READY' and host(existing['website'])==host(website):
                changes.update(personalization=dump(p),personalization_status=status,personalization_at=data['personalization_at'],description=data['description'])
            c.execute(candidates.update().where(candidates.c.id==ident).values(**changes))
        original=str(row.get('candidate_id') or row.get('profile_id') or row.get('id') or '')
        self.local_map[(str(path),original)]=ident
        return ident

    def contact(self,c,row,ident):
        if not ident:return
        email=str(row.get('email') or row.get('contact_email') or '').strip().lower()
        url=str(row.get('channel_url') or row.get('url') or '')
        raw=dump(row)
        if not email and not url:return
        payload={'id':sha(ident+raw),'candidate_id':ident,'kind':row.get('channel_type') or ('EMAIL' if email else 'CONTACT_FORM'),
                 'email':email,'url':url,'source_url':row.get('source_url'),'evidence':row.get('evidence') or row.get('evidence_text'),
                 'verification':row.get('review_status') or row.get('verification_status') or 'UNKNOWN',
                 'purpose':row.get('purpose'),'suppressed':False,'raw':raw}
        self.db.upsert(c,contacts,[payload],update=False)

    def rows(self,path,relation,iterator,count,kind='company',candidate_table=False,priority=False):
        source_id=sha(str(path)+':'+relation);offset=0
        # Resumption is safe only for unchanged sources; changed files are fully replayed idempotently.
        p=Path(path); fingerprint=sha(str(p.stat().st_size)+':'+str(p.stat().st_mtime_ns)) if p.exists() else ''
        with self.db.transaction() as c:
            previous=c.execute(select(sources).where(sources.c.id==source_id)).mappings().first()
            if previous and previous['fingerprint']==fingerprint:
                if previous['status']=='COMPLETE':return
                offset=previous['processed'] or 0
            self.db.upsert(c,sources,[dict(id=source_id,path=str(path),relation=relation,fingerprint=fingerprint,expected=count,processed=offset,status='RUNNING',updated_at=now())])
        batch=[];number=0
        def flush(batch,number):
            with self.db.transaction() as c:
                archived=[];membership=[]
                for key,row in batch:
                    ident=None;reason='';disposition='PRESERVED'
                    if relation in WORKFLOW or str(path).startswith('demo_runs/') or Path(path).name in WORKFLOW_EXPORTS:
                        disposition='ISOLATED';reason='Historical workflow; never replayed into the new lifecycle'
                    elif fixture(row):disposition='ISOLATED';reason='Explicit owner-controlled or synthetic fixture identity'
                    else:
                        if candidate_table and (row.get('company_name') or row.get('name')):
                            ident=self.candidate(c,row,kind,path,priority)
                            for contact in parse(row.get('contact_channels_json'),[]):
                                if isinstance(contact,dict):self.contact(c,contact,ident)
                        else:
                            original=str(row.get('candidate_id') or row.get('profile_id') or row.get('company_id') or '')
                            ident=self.local_map.get((str(path),original)) or (original if original in self.ids else None)
                            if 'contact' in relation and ident:self.contact(c,row,ident)
                            if relation=='suppressed_recipients' and row.get('email'):
                                self.db.upsert(c,suppressions,[dict(email=row['email'].lower(),reason='Legacy suppression',recorded_at=now())],keys=('email',))
                        if not ident:reason='Source-level record or unresolved identity; original row retained without reassignment'
                    raw=dump(row)
                    content_id=sha(str(ident)+':'+disposition+':'+raw)
                    archived.append(dict(id=content_id,source_id=source_id,source_key=str(key),candidate_id=ident,
                         disposition=disposition,reason=reason,checksum=sha(raw),raw=pack(raw)))
                    membership.append([str(key),content_id,ident])
                self.db.upsert(c,source_rows,archived,update=False)
                self.db.upsert(c,source_memberships,[dict(id=sha(source_id+':'+str(number-len(batch))),source_id=source_id,offset=number-len(batch),count=len(batch),payload=zlib.compress(dump(membership).encode()))])
                c.execute(sources.update().where(sources.c.id==source_id).values(processed=number,updated_at=now()))
        for key,row in iterator:
            number+=1
            if number<=offset:continue
            batch.append((key,row))
            if len(batch)>=self.batch_size:flush(batch,number);batch=[]
        if batch:flush(batch,number)
        with self.db.transaction() as c:
            c.execute(sources.update().where(sources.c.id==source_id).values(processed=number,status='COMPLETE',updated_at=now()))
        print(f'{path}:{relation}: {number}',flush=True)

    def sqlite(self,path,kind='company',priority=False):
        with readonly(path) as source:
            tables=[x[0] for x in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            if {'source_rows','source_memberships','candidates','jobs'}.issubset(set(tables)):
                raise ValueError('Refusing to import a rebuild operational database as a source')
            tables.sort(key=lambda t:(t not in {'candidates','companies','research_records'},t))
            # Restore source-local numeric ID mappings even when candidate import was checkpointed.
            with self.db.transaction() as c:
                for r in c.execute(select(source_memberships.c.payload).join(sources).where(sources.c.path==str(path),sources.c.relation.in_(['candidates','companies','research_records']))):
                    for key,_,ident in json.loads(zlib.decompress(r.payload)):
                        if ident:self.local_map[(str(path),key)]=ident
            for t in tables:
                if not t.replace('_','').isalnum():raise ValueError('Unexpected source table')
                count=source.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                columns=[r[1] for r in source.execute(f'PRAGMA table_info("{t}")')]
                pk=[r[1] for r in source.execute(f'PRAGMA table_info("{t}")') if r[5]]
                ordering=','.join('"'+x+'"' for x in pk) if pk else 'rowid'
                def iterate():
                    for n,r in enumerate(source.execute(f'SELECT * FROM "{t}" ORDER BY {ordering}')):
                        row=dict(r)
                        key=str(row[pk[0]]) if len(pk)==1 else dump([row[k] for k in pk]) if pk else str(n)
                        yield key,row
                self.rows(path,t,iterate(),count,kind,t in {'candidates','companies','research_records'},priority)

    def csv(self,path):
        with Path(path).open(encoding='utf-8-sig',newline='') as f:
            count=sum(1 for _ in csv.DictReader(f))
        with Path(path).open(encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f)
            is_candidate=bool({'company_name','name'} & set(reader.fieldnames or []))
            self.rows(path,'csv',enumerate(reader),count,'hotel' if Path(path).name.startswith('hotel_') else 'company',is_candidate)

    def report(self):
        with self.db.transaction() as c:
            result={'candidates':dict(c.execute(select(candidates.c.kind,func.count()).group_by(candidates.c.kind)).all()),
             'contacts':c.scalar(select(func.count()).select_from(contacts)),
             'personalization_ready':c.scalar(select(func.count()).select_from(candidates).where(candidates.c.personalization_status=='READY')),
             'source_memberships':c.scalar(select(func.sum(source_memberships.c.count))),
             'source_rows':c.scalar(select(func.count()).select_from(source_rows)),
             'isolated_rows':c.scalar(select(func.count()).select_from(source_rows).where(source_rows.c.disposition=='ISOLATED')),
             'sources':[dict(r) for r in c.execute(select(sources)).mappings()],
             'unmapped_preserved_rows':c.scalar(select(func.count()).select_from(source_rows).where(source_rows.c.disposition=='PRESERVED',source_rows.c.candidate_id.is_(None)))}
            result['counts_reconcile']=all(r['expected']==r['processed'] and r['status']=='COMPLETE' for r in result['sources'])
        return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--all-sources',action='store_true');p.add_argument('--cloud-snapshot');p.add_argument('--lean-operational-snapshot');p.add_argument('--archive-manifest');p.add_argument('--report',default='docs/rebuild/migration-reconciliation.json');a=p.parse_args()
    db=Database(a.url);db.initialize();m=Migrator(db)
    if a.lean_operational_snapshot:
        if not a.archive_manifest:p.error('--archive-manifest is required with --lean-operational-snapshot')
        result=m.lean_operational_snapshot(a.lean_operational_snapshot,archive_manifest=a.archive_manifest,
            expected={'candidates':{'company':71325,'hotel':15262},'contacts':52077,'isolated_rows':392,'source_memberships':2173051})
        Path(a.report).write_text(json.dumps(result,indent=2))
        print(json.dumps(result,indent=2));return
    m.sqlite(Path('data/phase7_candidates.db'))
    m.sqlite(Path('data/hotel_candidates.db'),'hotel')
    if a.cloud_snapshot:m.sqlite(Path(a.cloud_snapshot),priority=True)
    m.sqlite(Path('data/research_registry.db'),priority=True)
    if a.all_sources:
        for path in sorted(Path('data').glob('*.db')):
            if path.name not in {'phase7_candidates.db','hotel_candidates.db','research_registry.db'} and not path.name.startswith('outreach_v2'):m.sqlite(path)
        for path in sorted(Path('data').rglob('*.csv')):m.csv(path)
    Path(a.report).write_text(json.dumps(m.report(),indent=2))
if __name__=='__main__':main()
