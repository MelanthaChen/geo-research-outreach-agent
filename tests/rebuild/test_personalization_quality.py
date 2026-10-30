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
    p=observe('https://8dyer.com/',html,now(),name='8 Dyer')
    assert p['analysis_version']=='structured-evidence-v2'
    assert quality_reasons(p)==[]
    assert 'architecture and interior design studio' in p['company_strength']
    assert p['assessment']['geo_observation']=='NOT_ESTABLISHED'


def test_192books_real_page_relationship_has_traceable_claims_and_passes_gate():
    from outreach_v2.personalization import observe
    stamp=now()
    home='''<html><head><title>192 Books | 192 Tenth Avenue at 21st Street, New York</title></head>
    <body><main><p>Opened on May 22, 2003, 192 Books offers books of lasting interest on a variety of subjects, including literary fiction, works in translation, art and photography, film, poetry, music, history, biography, natural history, science, current affairs, and books for children and young adults.</p>
    <p>192 hosts readings by many distinguished writers and artists. Upcoming events can be found here; if you missed an event, you can watch a recording on YouTube.</p>
    <a href="https://www.youtube.com/@192Books/streams">YouTube</a>
    <a href="/upcoming-events/">Events</a><a href="/staff-picks/">Staff Picks</a><a href="/personal-library/">Personal Library</a>
    </main></body></html>'''
    events='''<html><head><title>EVENTS | 192 Books</title></head><body><main>
    <p>All readings are free, with no reserved seating; seating is limited and will be first come, first served. Readings start at 7pm, at 192 Books, 192 10th Ave. Archived recordings of readings are available on Youtube.</p>
    <a href="https://paulacoopergallery-studio.com/posts">Youtube</a>
    <h2>Wednesday, September 16, 6pm | Pamela Hanson: The 90s | a signing with Pamela Hanson</h2>
    <p>Pamela Hanson: The 90s: Quintessential nineties images taken from fashion editorials and behind-the-scenes candid moments.</p>
    </main></body></html>'''
    staff='''<html><head><title>STAFF PICKS | 192 Books</title></head><body><main>
    <h2>Matthew’s Pick</h2><p>Fall 2026—Books We Are Reading, Have Read, Recommend, or Want You to Read.</p>
    <p>Staff picks are available for purchase in our store and online.</p><p>Terrence’s Pick The Recognitions by William Gaddis.</p>
    </main></body></html>'''
    library='''<html><head><title>PERSONAL LIBRARY | 192 Books</title></head><body><main>
    <p>Are you seeking a well-curated, custom library or bookshelf for your office, home, or business? Contact info@192books.com and our team of booksellers will put together a list of the best art &amp; literary books to suit your needs. In-store pickup and delivery (within the United States) is available for books ordered through us.</p>
    </main></body></html>'''
    p=observe('http://www.192books.com/',home,stamp,name='192 Books',additional_pages=[
        ('http://www.192books.com/upcoming-events/',events,False),
        ('http://www.192books.com/staff-picks/',staff,False),
        ('http://www.192books.com/personal-library/',library,False),
    ])
    assert quality_reasons(p)==[]
    assert p['analysis_version']=='structured-evidence-v2'
    assert 'books spanning literary fiction' in p['business_description']
    assert 'May 22, 2003' not in p['business_description']
    assert len(p['business_description'])<180
    assert len(p['company_strength'])<240
    assert p['claim_support']['company_strength']
    assert p['recommendations']
    assert p['assessment']['geo_observation']=='SUPPORTED'
    assert 'may be complementary' in p['evidence_profile']['relationships'][0]['interpretation'].lower()
    assert len(p['evidence_profile']['pages'])==4
    assert p['review_decisions']['EVIDENCE_VALID']['passed'] is True
    assert p['review_decisions']['OUTREACH_REVIEW_READY']['passed'] is True
    _,body=__import__('outreach_v2.template',fromlist=['render']).render('192 Books','http://www.192books.com/',p,
        'https://forms.example/form?ref=preview','PREVIEW-ONLY',identity={'researcher_name':'Test Researcher',
        'university_affiliation':'Example University','reply_to':'researcher@example.edu'})
    assert 'sampled pages' not in body.casefold()
    assert 'These destinations may be complementary' in body
    assert 'paulacoopergallery-studio.com' not in body
    assert 'If these are complementary collections' not in body
    assert 'booksellers prepare tailored lists of art and literary books' in p['recipient_copy']['distinctive_detail']
    assert 'The sampled pages do not explain' not in body
    assert 'personal-library/' not in body
    assert 'art & literary books' in p['evidence_profile']['facts'][-1]['evidence_excerpt']
    assert p['review_decisions']['EVIDENCE_VALID']['passed'] is True
    assert p['review_decisions']['OUTREACH_REVIEW_READY']['passed'] is True
    validate(p,'http://www.192books.com/')


def test_single_authoritative_claim_is_enough_but_geo_observation_stays_separate():
    html='''<html><head><title>About North Shore Dental</title></head><body><main>
    <h1>About our practice</h1><p>North Shore Dental provides family dentistry, pediatric cleanings, same-day emergency appointments, and treatment plans reviewed by its two on-site dentists before major procedures.</p>
    </main></body></html>'''
    p=observe('https://northshore.example/about',html,now(),name='North Shore Dental')
    assert quality_reasons(p)==[]
    assert 'COMPANY_STATED' in {f.get('attribution') for f in p['evidence_profile']['facts']}
    assert p['recommendations']==[]
    assert p['assessment']['personalization_evidence']=='SUFFICIENT'
    assert p['assessment']['geo_observation']=='NOT_ESTABLISHED'
    assert p['assessment']['invitation']=='ELIGIBLE_FOR_REVIEW'
    assert p['assessment']['human_review_required'] is True
    validate(p,'https://northshore.example/about')
    from outreach_v2.template import render
    _,body=render('North Shore Dental','https://northshore.example/about',p,
        'https://forms.example/form?ref=preview','PREVIEW-ONLY',
        identity={'researcher_name':'Test Researcher','university_affiliation':'Example University',
                  'reply_to':'researcher@example.edu'})
    assert p['recipient_copy']['business_summary'] in body
    assert p['recipient_copy']['research_relevance'] in body
    for internal in ('Business description:', 'Company strength:', 'sampled pages', 'NOT_ESTABLISHED'):
        assert internal not in body
    assert 'OWNER-CONTROLLED STAGING TEST' not in body


def test_partial_secondary_403_does_not_discard_accessible_first_party_claims():
    import httpx
    stamp=now(); calls=[]
    home='''<html><head><title>Anima Pet Supply</title></head><body><main>
    <h1>Pet supply</h1><p>Anima Pet Supply's certified team offers tailored nutrition recommendations and in-store allergy testing for companion animals.</p>
    <a href="/about-us/">About Us</a><a href="/products/cats">Cat products</a></main></body></html>'''
    about='''<html><head><title>About Us | Anima Pet Supply</title></head><body><main>
    <h1>Our team</h1><p>Our certified team reviews pet health research, tracks product recalls, and provides tailored nutrition recommendations based on each animal's needs.</p>
    </main></body></html>'''
    def fetch(url):
        calls.append(url)
        if url.endswith('/'): return url,home,False
        if 'about-us' in url: return url,about,False
        request=httpx.Request('GET',url)
        response=httpx.Response(403,request=request)
        raise httpx.HTTPStatusError('Forbidden',request=request,response=response)
    p=__import__('outreach_v2.personalization',fromlist=['_sample'])._sample(fetch,'https://anima.example/',stamp,name='Anima Pet Supply')
    assert quality_reasons(p)==[]
    assert 'certified team' in p['company_strength'].casefold()
    assert 'tailored nutrition' in p['company_strength'].casefold()
    assert p['assessment']['geo_observation']=='NOT_ESTABLISHED'
    assert any('could not be accessed' in x for x in p['assessment']['limitations'])
    assert len(calls)==3
    from outreach_v2.template import render
    _,body=render('Anima Pet Supply','https://anima.example/',p,'https://forms.example/form','PREVIEW',
        record={'id':'anima-real','fixture':False,'name':'Anima Pet Supply','website':'https://anima.example/'},
        identity={'researcher_name':'Test Researcher','university_affiliation':'Example University','reply_to':'researcher@example.edu'})
    assert 'OWNER-CONTROLLED STAGING TEST' not in body
    assert p['review_decisions']['EVIDENCE_VALID']['passed'] is True
    assert p['review_decisions']['OUTREACH_REVIEW_READY']['passed'] is True

def test_custom_design_strength_is_a_concise_summary_of_multiple_supported_details():
    from outreach_v2.personalization import observe
    page='''<html><head><title>Services | LAGOON</title></head><body><main>
    <p>At Lagoon, both Amalya and Logan offer a custom design service within the styles they create. Select the silhouette of your choice and we can tailor the size and fabric/colors to you!</p>
    </main></body></html>'''
    p=observe('https://lagoon.example/services',page,now(),name='Lagoon')
    detail=p['recipient_copy']['distinctive_detail']
    assert detail=='Its custom-design service lets clients choose a silhouette and tailor the size, fabric, and colors.'
    assert len(detail)<130
    assert 'Select the silhouette' not in detail
    assert p['review_decisions']['EVIDENCE_VALID']['passed'] is True
    assert p['review_decisions']['OUTREACH_REVIEW_READY']['passed'] is True


def test_structured_profile_ignores_hidden_and_embedded_builder_paragraphs():
    from outreach_v2.personalization import _page_profile
    html='''<html><body><script type="application/json">{"description":"Builder payload must not become a company fact."}</script>
    <div hidden><p>Opened in 1980, Hidden Company offers imaginary services copied from a hidden template.</p></div>
    <main><h1>Visible Studio</h1><p>Visible Studio creates custom furniture for homes using locally sourced hardwood and traditional joinery.</p></main></body></html>'''
    profile=_page_profile('https://visible.example/',html)
    assert profile['paragraphs']==['Visible Studio creates custom furniture for homes using locally sourced hardwood and traditional joinery.']
    assert 'imaginary services' not in profile['visible_text']
    assert all('Builder payload' not in fact['value'] for fact in profile['structured_facts'])

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
        assert c.exec_driver_sql('SELECT count(*) FROM schema_migrations').scalar_one()==7
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
