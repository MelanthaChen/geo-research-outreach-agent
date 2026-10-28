"""Bounded, free HTTP evidence backfill for the frozen invitation variables only."""
from __future__ import annotations
import ipaddress,socket,uuid,re
from datetime import datetime,timedelta,timezone
from urllib.parse import urlparse,urljoin
import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select,or_,and_,func,exists
from .db import candidates,contacts,invitations,events,now
from .migrate import dump,parse
from .template import validate,same_site
from .service import email_contacts

def public_url(url):
    p=urlparse(url)
    if p.scheme not in {'https','http'} or not p.hostname or p.username or p.password:raise ValueError('Invalid public website URL')
    addresses=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(r[4][0]).is_global for r in addresses):raise ValueError('Website resolves to a non-public address')

def fetch_page(url):
    public_url(url)
    with httpx.Client(timeout=8,follow_redirects=False,headers={'User-Agent':'GEOResearchOutreach/2.0 (bounded invitation evidence)'}) as client:
        # Respect robots before fetching content; unavailable rules fail closed.
        from urllib.robotparser import RobotFileParser
        parsed=urlparse(url);origin=f'{parsed.scheme}://{parsed.netloc}'
        robots=client.get(origin+'/robots.txt')
        if robots.status_code==200:
            rules=RobotFileParser();rules.parse(robots.text.splitlines())
            if not rules.can_fetch('GEOResearchOutreach',url):raise ValueError('Website robots rules disallow this page')
        elif robots.status_code not in {404,410}:raise ValueError('Website robots policy could not be checked')
        target=url
        for _ in range(3):
            public_url(target)
            with client.stream('GET',target) as response:
                if response.is_redirect:
                    new=urljoin(target,response.headers.get('location',''))
                    if not same_site(url,new):raise ValueError('Redirect leaves the verified website')
                    target=new;continue
                response.raise_for_status()
                if 'text/html' not in response.headers.get('content-type',''):raise ValueError('Website did not return HTML')
                chunks=[];size=0
                for chunk in response.iter_bytes():
                    size+=len(chunk)
                    if size>512_000:raise ValueError('Website exceeds the bounded evidence size')
                    chunks.append(chunk)
                return target,b''.join(chunks).decode(response.encoding or 'utf-8',errors='replace')
        raise ValueError('Website redirect limit exceeded')

def observe(url,html,stamp,name=None):
    soup=BeautifulSoup(html,'html.parser')
    for tag in soup(['script','style','nav','footer']):tag.decompose()
    description=soup.find('meta',attrs={'name':'description'})
    description=(description.get('content','').strip() if description else '')
    body_text=soup.get_text(' ',strip=True)
    if not description:
        description=next((p.get_text(' ',strip=True) for p in soup.find_all('p') if 70<=len(p.get_text(' ',strip=True))<=700),'')
    if not description and len(body_text)>=100:description=body_text[:600]
    heading=soup.find('h1');heading=heading.get_text(' ',strip=True) if heading else ''
    if not description:raise ValueError('A grounded business description cannot yet be established from the sampled page')
    rec=[]
    # Also inspect the observed page copy. These findings are scoped to the
    # sampled page text, not claims about the entire website.
    visible=body_text.casefold()
    if not re.search(r'\b(faq|frequently asked questions|hours|pricing|price list|how to|what to)\b',visible):
        rec.append({'title':'Answer common customer questions',
            'problem':'No FAQ or direct answers about hours, pricing, or common customer questions appear in the sampled homepage text.',
            'suggestion':'Add a concise factual section that answers the questions customers need before choosing the business.',
            'evidence_excerpt':body_text[:500]})
    service_words=r'\b(products?|services?|solutions?|menu|repairs?|classes|appointments?|rentals?|consulting|treatments?)\b'
    if not re.search(service_words,visible):
        rec.append({'title':'Describe the specific offering',
            'problem':'The sampled homepage copy does not explicitly name a product, service, or offering.',
            'suggestion':'State the main offering in plain language and connect it to the customer need it serves.',
            'evidence_excerpt':body_text[:500]})
    if not soup.find('title') or not soup.find('title').get_text(strip=True):
        rec.append({'title':'Page title','problem':'The sampled homepage has no non-empty HTML title.','suggestion':'Provide a concise title that identifies the business and the page topic.'})
    if not soup.find('meta',attrs={'name':'description'}):
        rec.append({'title':'Page description','problem':'The sampled homepage has no meta description.','suggestion':'Add an accurate description of the business and the purpose of this page.'})
    # Do not invent a problem when the bounded page gives no supported finding.
    if not rec:raise ValueError('No V1 recommendation is supported by this minimal sample; preserve existing data and defer')
    for r in rec:r.update(source_urls=[url],observed_at=stamp)
    strength=(f'The sampled homepage explicitly presents this heading: “{heading}”.' if heading
              else f'The sampled homepage includes factual descriptive text: “{description[:240]}”.')
    return {'data_classification':'OBSERVED','business_description':description,
       'company_strength':strength,
       'source_urls':[url],'retrieved_at':stamp,'recommendations':rec[:2],
       'evidence':{'description_quote':description,'heading_quote':heading,'visible_text_excerpt':body_text[:1200],'sampled_urls':[url]}}

def backfill(db,limit=25,fetcher=fetch_page):
    if not 1<=limit<=100:raise ValueError('Backfill request budget must be between 1 and 100 websites')
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=6)).isoformat()
    first_party_email_count=select(func.count(func.distinct(contacts.c.email))).where(email_contacts()).correlate(candidates).scalar_subquery()
    no_prior_invitation=~exists(select(invitations.c.id).where(invitations.c.candidate_id==candidates.c.id))
    with db.transaction() as c:
        rows=[dict(r) for r in c.execute(select(candidates).where(candidates.c.kind=='company',candidates.c.fixture.is_(False),
             candidates.c.website_status=='VERIFIED',
             candidates.c.qualification.in_(['ELIGIBLE','QUALIFIED']),candidates.c.suppressed.is_(False),
             candidates.c.personalization_status!='READY',
             or_(candidates.c.personalization_at.is_(None),candidates.c.personalization_at<cutoff),
             first_party_email_count==1,no_prior_invitation).order_by(candidates.c.personalization_at,candidates.c.id).limit(limit)).mappings()]
    result={'completed':0,'incomplete':0}
    for row in rows:
        stamp=now()
        try:
            url,html=fetcher(row['website']);p=observe(url,html,stamp,row.get('name'));validate(p,row['website'])
            values={'personalization':dump(p),'personalization_status':'READY','personalization_at':stamp,'personalization_error':None,'description':p['business_description']}
            result['completed']+=1
        except Exception as e:
            values={'personalization_status':'INCOMPLETE','personalization_at':stamp,'personalization_error':str(e)[:300] if isinstance(e,ValueError) else type(e).__name__}
            result['incomplete']+=1
        with db.transaction() as c:
            c.execute(candidates.update().where(candidates.c.id==row['id'],candidates.c.personalization_status!='READY').values(**values))
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=row['id'],event='PERSONALIZATION_'+values['personalization_status'],detail=dump({'error':values.get('personalization_error'),'website':row['website'],'source_ref':row.get('source_ref')}),created_at=stamp))
    return result
