"""Bounded, free HTTP evidence backfill for the frozen invitation variables only."""
from __future__ import annotations
import ipaddress,socket,uuid
from urllib.parse import urlparse,urljoin
import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select,or_
from .db import candidates,events,now
from .migrate import dump,parse
from .template import validate,same_site

def public_url(url):
    p=urlparse(url)
    if p.scheme not in {'https','http'} or not p.hostname or p.username or p.password:raise ValueError('Invalid public website URL')
    addresses=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(r[4][0]).is_global for r in addresses):raise ValueError('Website resolves to a non-public address')

def fetch_page(url):
    public_url(url)
    with httpx.Client(timeout=20,follow_redirects=False,headers={'User-Agent':'GEOResearchOutreach/2.0 (bounded invitation evidence)'}) as client:
        # Respect robots before fetching content; unavailable rules fail closed.
        from urllib.robotparser import RobotFileParser
        parsed=urlparse(url);origin=f'{parsed.scheme}://{parsed.netloc}'
        robots=client.get(origin+'/robots.txt')
        if robots.status_code==200:
            rules=RobotFileParser();rules.parse(robots.text.splitlines())
            if not rules.can_fetch('GEOResearchOutreach',url):raise ValueError('Website robots rules disallow this page')
        elif robots.status_code not in {404,410}:raise ValueError('Website robots policy could not be checked')
        target=url
        for _ in range(4):
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
                    if size>2_000_000:raise ValueError('Website exceeds the bounded evidence size')
                    chunks.append(chunk)
                return target,b''.join(chunks).decode(response.encoding or 'utf-8',errors='replace')
        raise ValueError('Website redirect limit exceeded')

def observe(url,html,stamp):
    soup=BeautifulSoup(html,'html.parser')
    for tag in soup(['script','style','nav','footer']):tag.decompose()
    description=soup.find('meta',attrs={'name':'description'})
    description=(description.get('content','').strip() if description else '')
    if not description:
        description=next((p.get_text(' ',strip=True) for p in soup.find_all('p') if 70<=len(p.get_text(' ',strip=True))<=700),'')
    heading=soup.find('h1');heading=heading.get_text(' ',strip=True) if heading else ''
    if not description or not heading:raise ValueError('A grounded business description and company strength cannot yet be established')
    rec=[]
    if not soup.find('title') or not soup.find('title').get_text(strip=True):
        rec.append({'title':'Page title','problem':'The sampled homepage has no non-empty HTML title.','suggestion':'Provide a concise title that identifies the business and the page topic.'})
    if not soup.find('meta',attrs={'name':'description'}):
        rec.append({'title':'Page description','problem':'The sampled homepage has no meta description.','suggestion':'Add an accurate description of the business and the purpose of this page.'})
    # Do not invent a problem when the bounded page gives no supported finding.
    if not rec:raise ValueError('No V1 recommendation is supported by this minimal sample; preserve existing data and defer')
    for r in rec:r.update(source_urls=[url],observed_at=stamp)
    return {'data_classification':'OBSERVED','business_description':description,
       'company_strength':f'The homepage explicitly presents its offering as “{heading}”.',
       'source_urls':[url],'retrieved_at':stamp,'recommendations':rec[:2],
       'evidence':{'description_quote':description,'heading_quote':heading,'sampled_urls':[url]}}

def backfill(db,limit=25,fetcher=fetch_page):
    if not 1<=limit<=100:raise ValueError('Backfill request budget must be between 1 and 100 websites')
    with db.transaction() as c:
        rows=[dict(r) for r in c.execute(select(candidates).where(candidates.c.website_status=='VERIFIED',
             candidates.c.qualification.in_(['ELIGIBLE','QUALIFIED']),candidates.c.suppressed.is_(False),
             candidates.c.personalization_status!='READY').order_by(candidates.c.personalization_at,candidates.c.id).limit(limit)).mappings()]
    result={'completed':0,'incomplete':0}
    for row in rows:
        stamp=now()
        try:
            url,html=fetcher(row['website']);p=observe(url,html,stamp);validate(p,row['website'])
            values={'personalization':dump(p),'personalization_status':'READY','personalization_at':stamp,'personalization_error':None,'description':p['business_description']}
            result['completed']+=1
        except Exception as e:
            values={'personalization_status':'INCOMPLETE','personalization_at':stamp,'personalization_error':str(e)[:300] if isinstance(e,ValueError) else type(e).__name__}
            result['incomplete']+=1
        with db.transaction() as c:
            c.execute(candidates.update().where(candidates.c.id==row['id'],candidates.c.personalization_status!='READY').values(**values))
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=row['id'],event='PERSONALIZATION_'+values['personalization_status'],detail=values.get('personalization_error'),created_at=stamp))
    return result
