from __future__ import annotations
import json
from outreach_v2.personalization import _page_text, observe
from outreach_v2.quality import quality_reasons
from outreach_v2.template import validate
from outreach_v2.db import Database, candidates, contacts, now
from outreach_v2.migrate import dump
from outreach_v2.service import readiness
from sqlalchemy import select

def test_squarespace_8dyer_pattern_drops_builder_code_and_preserves_visible_evidence():
    html='''<html><head>
    <script>window.Static = {"appState":{"site":{"id":"secret-config"}}};</script>
    <script type="application/json">{"siteId":"CONFIG","template":"squarespace"}</script>
    <style>.sqs-block { display: none; } body { font-family: sans-serif; }</style>
    <template><div>Template preview company description</div></template></head>
    <body><header><h1>8 Dyer</h1></header><main><p>8 Dyer is a New York architecture and interior design studio focused on residential renovation and carefully detailed spaces.</p>
    <section><h2>Selected work</h2><p>The studio documents completed residential projects with location and project descriptions.</p></section></main></body></html>'''
    soup,text=_page_text(html)
    assert 'architecture and interior design studio' in text
    for forbidden in ('window.Static','appState','siteId','squarespace','.sqs-block','font-family','Template preview'):
        assert forbidden not in text
    result=observe('https://8dyer.com/',html,now(),name='8 Dyer')
    assert result['source_urls']==['https://8dyer.com/']
    assert 'residential renovation' in result['business_description']
    assert 'GENERIC_STRENGTH' in quality_reasons(result)

def test_generic_descriptions_strengths_and_recommendations_are_rejected():
    p={'data_classification':'OBSERVED','business_description':'The bounded sample contains factual business and service information.',
       'company_strength':'The bounded sample contains factual business and service information.',
       'source_urls':['https://example.org/'],'retrieved_at':now(),
       'recommendations':[{'title':'Improve GEO','problem':'The bounded sample contains factual business and service information.',
          'suggestion':'Improve your website for AI visibility.','evidence_excerpt':'Generic observation.','source_urls':['https://example.org/'],'observed_at':now()}]}
    failures=quality_reasons(p)
    assert 'INSUFFICIENT_EVIDENCE' in failures
    assert 'GENERIC_STRENGTH' in failures
    assert 'GENERIC_RECOMMENDATION' in failures
    try: validate(p,'https://example.org/')
    except ValueError as exc:
        assert 'GENERIC_STRENGTH' in str(exc) and 'GENERIC_RECOMMENDATION' in str(exc)
    else: raise AssertionError('Generic personalization unexpectedly passed validation')

def test_8dyer_raw_squarespace_payload_is_contamination_and_valid_specific_evidence_passes():
    contaminated={'data_classification':'OBSERVED','business_description':'8 Dyer is a studio. window.Static = {"siteId":"x"}; .sqs-block { display:none; }',
       'company_strength':'The bounded sample contains factual business and service information.',
       'source_urls':['https://8dyer.com/'],'retrieved_at':now(),
       'evidence':{'visible_text_excerpt':'Visible studio description.'},
       'recommendations':[{'title':'Improve GEO','problem':'The content is generic.','suggestion':'Improve your website for AI visibility.',
          'evidence_excerpt':'Generic observation.','source_urls':['https://8dyer.com/'],'observed_at':now()}]}
    failures=quality_reasons(contaminated)
    assert 'CODE_CONTAMINATION' in failures
    assert 'GENERIC_STRENGTH' in failures and 'GENERIC_RECOMMENDATION' in failures
    valid={'data_classification':'OBSERVED','business_description':'North Shore Dental provides family dentistry, teeth cleaning, and emergency appointments from its Salem clinic.',
       'company_strength':'Its service page names family dentistry, cleaning, and emergency appointments and gives the clinic location.',
       'source_urls':['https://northshore.example/services'],'retrieved_at':now(),
       'evidence':{'visible_text_excerpt':'Services: family dentistry, teeth cleaning, emergency appointments. Location: Salem.'},
       'recommendations':[{'title':'Label emergency appointments','problem':'The services page lists emergency appointments but does not say whether patients should call or use the booking form.',
          'suggestion':'Add a sentence beside the emergency appointments listing that tells patients whether to call or book online.',
          'evidence_excerpt':'The page lists emergency appointments and a separate online booking link without connecting the two.',
          'source_urls':['https://northshore.example/services'],'observed_at':now()}]}
    assert quality_reasons(valid)==[]

def test_legacy_ready_rows_default_to_unverified_and_cannot_enter_selection(tmp_path):
    db=Database('sqlite:///'+str(tmp_path/'legacy.db'));db.initialize()
    p={'data_classification':'OBSERVED','business_description':'An older company description with no supporting evidence.',
       'company_strength':'A company strength statement that was never quality checked.',
       'source_urls':['https://example.org'],'retrieved_at':now(),'recommendations':[]}
    with db.transaction() as c:
        c.execute(candidates.insert().values(id='legacy',kind='company',name='Legacy Company',website='https://example.org',
            domain='example.org',website_status='VERIFIED',qualification='ELIGIBLE',suppressed=False,
            personalization=dump(p),personalization_status='READY',fixture=False,created_at=now(),updated_at=now()))
        c.execute(contacts.insert().values(id='legacy-contact',candidate_id='legacy',kind='EMAIL',email='info@example.org',
            source_url='https://example.org/contact',verification='VERIFIED',suppressed=False))
        assert c.scalar(select(readiness()).select_from(candidates).where(candidates.c.id=='legacy')) is False
        row=c.execute(select(candidates).where(candidates.c.id=='legacy')).mappings().one()
    assert row['personalization_quality_status']=='UNVERIFIED'
    from outreach_v2.quality_audit import audit
    report=audit(db)
    assert report['ready_records']==1 and report['ready_quality_failures']==1 and report['historical_rows_modified']==0
    with db.transaction() as c:
        after=c.execute(select(candidates.c.personalization_status,candidates.c.personalization_quality_status,
            candidates.c.personalization).where(candidates.c.id=='legacy')).one()
    assert after.personalization_status=='READY' and after.personalization_quality_status=='UNVERIFIED'
    db.engine.dispose()

def test_v5_upgrade_assesses_existing_ready_content_without_overwriting_it(tmp_path):
    import sqlite3
    path=tmp_path/'v4.db'
    raw='{"data_classification":"OBSERVED","business_description":"The bounded sample contains factual business and service information.","company_strength":"The bounded sample contains factual business and service information.","source_urls":["https://example.org"],"retrieved_at":"2026-01-01","recommendations":[]}'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)')
        c.executemany('INSERT INTO schema_migrations VALUES(?,?,?)',[(i,f'v{i}','2026-01-01') for i in range(1,5)])
        c.execute('CREATE TABLE candidates (id TEXT PRIMARY KEY, personalization TEXT, website TEXT, personalization_status TEXT)')
        c.execute('INSERT INTO candidates VALUES(?,?,?,?)',('old-ready',raw,'https://example.org','READY'))
    db=Database('sqlite:///'+str(path));db.initialize()
    with db.engine.connect() as c:
        result=c.exec_driver_sql('SELECT personalization,personalization_status,personalization_quality_status,personalization_quality_errors FROM candidates WHERE id=?',('old-ready',)).one()
        assert c.exec_driver_sql('SELECT count(*) FROM schema_migrations').scalar_one()==5
    assert result.personalization==raw and result.personalization_status=='READY'
    assert result.personalization_quality_status=='FAIL' and 'GENERIC_STRENGTH' in result.personalization_quality_errors
    db.engine.dispose()

def test_recommendation_requires_page_specific_evidence_and_concrete_action():
    p={'data_classification':'OBSERVED','business_description':'Whisker Haven offers boarding and grooming services for cats and dogs.',
       'company_strength':'Its service page distinguishes cat boarding from dog grooming and describes both offerings.',
       'source_urls':['https://whisker.example/'],'retrieved_at':now(),
       'recommendations':[{'title':'Improve','problem':'The site could be clearer.','suggestion':'Add more content.',
          'evidence_excerpt':'No specific evidence.','source_urls':['https://whisker.example/'],'observed_at':now()}]}
    assert 'GENERIC_RECOMMENDATION' in quality_reasons(p)
