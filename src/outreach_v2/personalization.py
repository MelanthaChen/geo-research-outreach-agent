"""Bounded, free HTTP evidence backfill for frozen invitation variables."""
from __future__ import annotations

import concurrent.futures
import ipaddress
import re
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import and_, exists, func, or_, select

from .db import (candidates, contacts, events, invitations, now, personalization_runs,
                 personalization_attempts, personalization_versions)
from .migrate import dump
from .service import email_contacts
from .template import same_site, validate
from .quality import CODE_MARKERS
from .replay_policy import replay_allowed_for_contract

MAX_PAGE_BYTES = 512_000
MAX_PILOT_CANDIDATES = 5
MAX_PILOT_SPEND_USD = '0.25'
# A fourth same-site page gives evidence-limited companies another grounded
# source opportunity while preserving the strict per-page size and time bounds.
MAX_PAGES_PER_CANDIDATE = 4
MAX_REDIRECTS_PER_PAGE = 5
LEASE_SECONDS = 300


def public_url(url):
    p = urlparse(url)
    if p.scheme not in {'https', 'http'} or not p.hostname or p.username or p.password:
        raise ValueError('Invalid public website URL')
    addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(r[4][0]).is_global for r in addresses):
        raise ValueError('Website resolves to a non-public address')


def fetch_page(url):
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False,
                       headers={'User-Agent': 'GEOResearchOutreach/2.0 (bounded invitation evidence)'}) as client:
        from urllib.robotparser import RobotFileParser
        def robots_allows(target):
            public_url(target)
            parsed = urlparse(target)
            origin = f'{parsed.scheme}://{parsed.netloc}'
            robots_url = origin + '/robots.txt'
            visited_robots = set()
            for redirect_count in range(MAX_REDIRECTS_PER_PAGE + 1):
                if robots_url in visited_robots:
                    raise ValueError('Website robots redirect loop detected')
                visited_robots.add(robots_url)
                public_url(robots_url)
                robots = client.get(robots_url)
                if robots.is_redirect:
                    location = robots.headers.get('location')
                    if not location or redirect_count == MAX_REDIRECTS_PER_PAGE:
                        raise ValueError('Website robots policy could not be checked')
                    robots_url = urljoin(robots_url, location)
                    public_url(robots_url)
                    continue
                break
            if robots.status_code == 200:
                rules = RobotFileParser()
                rules.parse(robots.text.splitlines())
                if not rules.can_fetch('GEOResearchOutreach', target):
                    raise ValueError('Website robots rules disallow this page')
            elif robots.status_code not in {404, 410}:
                # Never follow robots redirects or treat an unknown policy as
                # permission; retain HTTP errors for bounded transient retry.
                robots.raise_for_status()
                raise ValueError('Website robots policy could not be checked')
        target = url
        visited = set()
        for hop in range(MAX_REDIRECTS_PER_PAGE + 1):
            if target in visited:
                raise ValueError('Website redirect loop detected')
            visited.add(target)
            if not same_site(url, target):
                raise ValueError('Redirect leaves the verified website')
            robots_allows(target)
            public_url(target)
            with client.stream('GET', target) as response:
                if response.is_redirect:
                    new = urljoin(target, response.headers.get('location', ''))
                    if not same_site(url, new):
                        raise ValueError('Redirect leaves the verified website')
                    public_url(new)
                    if hop == MAX_REDIRECTS_PER_PAGE:
                        raise ValueError('Website redirect limit exceeded')
                    target = new
                    continue
                response.raise_for_status()
                if not same_site(url, str(response.url)):
                    raise ValueError('Final response leaves the verified website')
                if 'text/html' not in response.headers.get('content-type', ''):
                    raise ValueError('Website did not return HTML')
                chunks, size, truncated = [], 0, False
                for chunk in response.iter_bytes():
                    remaining = MAX_PAGE_BYTES - size
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        size += remaining
                        truncated = True
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                return target, b''.join(chunks).decode(response.encoding or 'utf-8', errors='replace'), truncated
        raise ValueError('Website redirect limit exceeded')


def _page_text(html):
    soup = BeautifulSoup(html, 'html.parser')
    # Site builders often place large app state, hydration payloads and editor
    # templates beside the rendered page. Never treat those as visible copy.
    structured=[]
    for script in soup.find_all('script', attrs={'type': re.compile(r'application/ld\+json',re.I)}):
        try:
            import json
            payload=json.loads(script.string or script.get_text())
            nodes=payload if isinstance(payload,list) else [payload]
            for node in nodes:
                if isinstance(node,dict):
                    for key in ('name','description','slogan'):
                        if isinstance(node.get(key),str): structured.append(node[key])
        except (ValueError,TypeError):
            pass
    for tag in soup(['script', 'style', 'template', 'noscript', 'svg', 'canvas', 'iframe', 'object', 'embed', 'nav', 'footer', 'code', 'pre']):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        # BeautifulSoup can leave decomposed descendants with attrs=None when
        # builders contain nested hidden templates. Treat those as empty.
        attrs = tag.attrs or {}
        style=(attrs.get('style') or '').replace(' ','').lower()
        if 'hidden' in attrs or attrs.get('aria-hidden')=='true' or 'display:none' in style or 'visibility:hidden' in style:
            tag.decompose()
    text=' '.join(soup.get_text(' ', strip=True).split())
    if structured:
        text=' '.join(dict.fromkeys([text,*structured]))
    return soup, text


def _evidence_urls(home_url, html):
    """Follow only explicit, relevant links from the verified homepage."""
    soup = BeautifulSoup(html, 'html.parser')
    # Keep the site's own information architecture: links are selected from
    # actual visible labels and same-site URLs, including editorial and event
    # pages that are relevant for non-commerce companies.
    priorities = ('about', 'contact', 'mission', 'community', 'program', 'event',
                  'staff', 'service', 'product', 'solution', 'library', 'faq',
                  'question', 'pricing', 'menu', 'book')
    found = []
    for link in soup.find_all('a', href=True):
        target = urljoin(home_url, link['href'].strip())
        label = ' '.join((link.get_text(' ', strip=True), urlparse(target).path)).casefold()
        if same_site(home_url, target) and any(word in label for word in priorities):
            normalized = target.split('#', 1)[0]
            if normalized not in found and normalized != home_url:
                found.append(normalized)
    found.sort(key=lambda item: (min((priorities.index(word) for word in priorities
                                      if word in (urlparse(item).path + ' ' + item).casefold()), default=99), item))
    return found[:MAX_PAGES_PER_CANDIDATE - 1]


def _page_profile(page_url, html, truncated=False):
    """Extract a bounded, inspectable profile from one fetched first-party page."""
    original = BeautifulSoup(html, 'html.parser')
    title = original.title.get_text(' ', strip=True) if original.title else ''
    visible_soup, visible_text = _page_text(html)
    # Use the pruned rendered-content tree for claims, links and headings. The
    # raw parse below is retained only to read typed JSON-LD business facts.
    headings = [' '.join(x.get_text(' ', strip=True).split()) for x in visible_soup.find_all(re.compile(r'^h[1-6]$'))]
    paragraphs = [' '.join(x.get_text(' ', strip=True).split()) for x in visible_soup.find_all('p')]
    sections = []
    active_heading = title
    for node in visible_soup.find_all(re.compile(r'^(h[1-6]|p)$')):
        if re.fullmatch(r'h[1-6]', node.name or ''):
            active_heading = ' '.join(node.get_text(' ', strip=True).split()) or title
        elif node.name == 'p':
            text = ' '.join(node.get_text(' ', strip=True).split())
            if len(text) >= 35 and not re.search(r'newsletter|leave this field empty|follow us', text, re.I):
                sections.append({'heading': active_heading, 'text': text})
    # Wix, catalog, and storefront pages often publish their visible claims in
    # div/span/list blocks rather than <p>. Preserve those on-page statements
    # as evidence, but select only leaf-like blocks so parent containers do not
    # duplicate the same copy. The HTML was already pruned of nav, footer,
    # hidden, script, and embedded content above.
    block_tags = {'li', 'div', 'section', 'article', 'main', 'span', 'td', 'dd'}
    existing_text = {section['text'] for section in sections}
    for node in visible_soup.find_all(True):
        if re.fullmatch(r'h[1-6]', node.name or ''):
            active_heading = ' '.join(node.get_text(' ', strip=True).split()) or title
            continue
        if node.name not in block_tags:
            continue
        if node.find_parent('p'):
            continue
        text = ' '.join(node.get_text(' ', strip=True).split())
        if len(text) < 35 or text in existing_text or re.search(r'newsletter|leave this field empty|follow us', text, re.I):
            continue
        if any(child.name in block_tags and len(' '.join(child.get_text(' ', strip=True).split())) >= 35
               for child in node.find_all(True)):
            continue
        # A block containing a paragraph already has a more exact evidence
        # boundary and must not become a second, broader claim.
        if node.name != 'li' and node.find('p'):
            continue
        sections.append({'heading': active_heading, 'text': text})
        existing_text.add(text)
    links = []
    for a in visible_soup.find_all('a', href=True):
        label = ' '.join(a.get_text(' ', strip=True).split())
        target = urljoin(page_url, a['href'].strip())
        if label and target.startswith(('http://', 'https://')):
            links.append({'label': label[:180], 'url': target})

    # JSON-LD contributes only typed business facts; the script source itself
    # is never exposed as visible copy or as a business description.
    structured = []
    def walk(node):
        if isinstance(node, dict):
            for key in ('name', 'description', 'address', 'telephone', 'openingHours', 'priceRange', 'email'):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    structured.append({'field': key, 'value': value.strip(), 'source_url': page_url})
                elif isinstance(value, dict):
                    for subkey in ('streetAddress', 'addressLocality', 'addressRegion', 'postalCode', 'addressCountry'):
                        subvalue = value.get(subkey)
                        if isinstance(subvalue, str) and subvalue.strip():
                            structured.append({'field': f'address.{subkey}', 'value': subvalue.strip(), 'source_url': page_url})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node: walk(value)
    import json
    for script in original.find_all('script', attrs={'type': re.compile(r'application/ld\+json', re.I)}):
        try: walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError): continue

    useful_paragraphs = [section['text'] for section in sections[:100]]
    path = urlparse(page_url).path.casefold()
    if 'event' in path: page_type = 'events'
    elif 'staff' in path or 'pick' in path: page_type = 'editorial'
    elif any(word in path for word in ('service','library','appointment','booking')): page_type = 'services'
    elif any(word in path for word in ('product','shop','collection','menu')): page_type = 'products'
    elif 'about' in path: page_type = 'about'
    else: page_type = 'home_or_other'
    return {'url': page_url, 'type': page_type, 'title': title, 'headings': headings[:40],
            'paragraphs': useful_paragraphs[:80], 'sections': sections[:100], 'links': links[:250],
            'structured_facts': structured[:80], 'visible_text': visible_text[:10000],
            'truncated': bool(truncated)}


def _sentences(text):
    return [part.strip() for part in re.split(r'(?<=[.!?])\s+(?=[A-Z0-9“\'])', text or '') if part.strip()]


def _first_party(text, name):
    return bool((name and re.search(rf'\b{re.escape(name)}\b', text, re.I)) or
                re.search(r'\b(?:we|our|us|my|i)\b', text, re.I))


def _content_terms(text):
    ignored={'about','what','offer','welcome','page','home','business','company','team','customers','people',
             'their','this','that','with','from','they','them','your','have','will','which','when','where',
             'more','most','many','some','other','also','into','than','then','there','here','yourself'}
    return {word.casefold().strip("'’") for word in re.findall(r"[A-Za-z][A-Za-z’'-]{2,}", text)
            if word.casefold().strip("'’") not in ignored}


def _possessive(name):
    return name+('’' if str(name).rstrip().casefold().endswith('s') else '’s')


def _recipient_summary(name, sentence, heading='', purpose='description'):
    """Turn a short first-party evidence sentence into concise recipient copy."""
    text=' '.join((sentence or '').split()).strip(' “”"')
    # Page headings and dated introductions are not part of the business claim.
    if heading:
        text=re.sub(r'^'+re.escape(heading)+r'\s*', '', text, flags=re.I)
    text=re.sub(r'^Welcome to\s+[^.!?]{1,50}\s+(?=[A-Z])', '', text, flags=re.I)
    text=re.sub(r'^Opened on [^,]+,\s*\d{4},\s*', '', text, flags=re.I)
    text=re.sub(r'^The Meaning Behind [^ ]+\s*', '', text, flags=re.I)
    text=re.sub(r'^Our Background and Motivations\s*', '', text, flags=re.I)
    text=re.sub(r'^What We Offer\s*', '', text, flags=re.I)

    # Prefer the business-identifying clause over a long founder introduction.
    identified=re.search(r'[^,]{2,90},\s*((?:[A-Z][\w& .\'-]{1,65})\s+is\s+(?:a|an|the)\s+.+?\s+based in\s+([^,.]+)(?:,\s*([A-Z]{2}))?)\.?$', text)
    if identified:
        core=identified.group(1)
        place=identified.group(2).strip()
        state=identified.group(3)
        core=re.sub(r'\s+based in\s+[^,.]+(?:,\s*[A-Z]{2})?$', '', core)
        text=f'{core} in {place}'+(f', {state}' if state else '')+'.'

    # Compress extensive catalog lists while retaining concrete examples.
    text=re.sub(r' offers books of lasting interest on a variety of subjects, including ',
                ' offers books spanning ', text, flags=re.I)
    if purpose=='description' and ' books spanning ' in text and len(text)>150:
        before,items=text.split(' books spanning ',1)
        values=[x.strip(' .') for x in re.split(r',\s*',items.rstrip('.')) if x.strip()]
        text=before+' books spanning '+', '.join(values[:4])+(' and more' if len(values)>4 else '')+'.'
    if purpose=='description' and re.match(r'^We focus on ',text,re.I):
        text=re.sub(r', and a profound respect for the deep bond.*?(?=\.|$)', '', text, flags=re.I)

    # Convert first-person business claims to a neutral third-person summary.
    text=re.sub(r'^We focus on ',f'{name} focuses on ',text,flags=re.I)
    text=re.sub(r'^We specialize in ',f'{name} specializes in ',text,flags=re.I)
    text=re.sub(r'^We offer ',f'{name} offers ',text,flags=re.I)
    text=re.sub(r'^([^.]{1,100}?)\s+provides\s+',r'\1 offers ',text,flags=re.I)
    text=re.sub(r'^At '+re.escape(name)+r',\s*',f'{name} ',text,flags=re.I)
    text=re.sub(r'^Our (certified|trained|experienced|specialist) team',
                lambda m:f'{name}’s {m.group(1).lower()} team',text,flags=re.I)
    text=re.sub(r'^Our team',f'{name}’s team',text,flags=re.I)
    text=re.sub(r'^Our ',f'{name}’s ',text,flags=re.I)
    text=re.sub(r'\byour pet\b','pets',text,flags=re.I)
    text=re.sub(r'\byour unique needs\b','individual needs',text,flags=re.I)

    # Remove promotional or philosophical tail clauses when an earlier
    # concrete product/service statement already gives the relevant fact.
    if purpose=='description':
        text=re.sub(r', and a profound respect for the deep bond.*?(?=\.|$)', '', text, flags=re.I)
        text=re.sub(r', giving you (?:a|an) (?:unique|special|wonderful) experience.*?(?=\.|$)', '', text, flags=re.I)
    text=' '.join(text.split())
    if len(text)>290:
        clauses=re.split(r';\s|,\s(?=[A-Z])',text)
        text=clauses[0].rstrip(' ,;')+'.'
    return text


def _distinctive_summary(name, sentence, heading='', context=''):
    specialist=re.search(r'\btrained in (?P<field>[\w -]{2,45}?)\s+programs?\b',context,re.I)
    tailored=re.search(r"\btailored recommendations? based on (?:your |each )?(?P<recipient>[\w -]{2,35}?)(?:'s|’s)?(?: unique)? needs",context,re.I)
    if specialist and tailored and re.search(r'\bcertified\b',context,re.I):
        field=specialist.group('field').strip().lower()
        recipient=tailored.group('recipient').strip().lower()
        recipient=re.sub(r'^(?:your|each)\s+','',recipient)
        return f'{_possessive(name)} certified team pairs {field} training with recommendations tailored to each {recipient}.'
    if (re.search(r'\bcustom design service\b',context,re.I) and
        re.search(r'\bselect the silhouette\b',context,re.I) and
        re.search(r'\btailor the size and fabric',context,re.I)):
        return 'Its custom-design service lets clients choose a silhouette and tailor the size, fabric, and colors.'
    library=re.search(r'custom\s+(?P<thing>[\w -]{3,35}?)\s+(?:or\s+(?P<alternate>[\w -]{3,25})\s+)?for your\s+(?P<places>[^?]{3,80})\?',context,re.I)
    team=re.search(r'our team of (?P<role>[\w -]{3,35}) will put together a list of the best (?P<items>[\w& -]{3,55}) to suit your needs',context,re.I)
    if library and team:
        items=re.sub(r'\s*&\s*',' and ',team.group('items').strip())
        items=re.sub(r'\s+books$','',items,flags=re.I)
        places=library.group('places').strip()
        if re.fullmatch(r'office, home, or business',places,re.I):
            places='offices, homes, and businesses'
        else:
            places=re.sub(r'\s*,\s*or\s+', ', and ',places)
        return f'Its booksellers prepare tailored lists of {items} books for {places}.'
    text=_recipient_summary(name,sentence,heading,purpose='strength')
    if re.match(r'^We diligently track ',text,re.I):
        text=re.sub(r'^We diligently track ',f'{_possessive(name)} team tracks ',text,flags=re.I)
        text=re.sub(r'\breview global pet health research\b','reviews pet-health research',text,flags=re.I)
        text=re.sub(r'\bstay current on evolving standards\b','follows changing standards',text,flags=re.I)
    elif re.match(r'^We ',text,re.I):
        text=re.sub(r'^We ',f'{name} ',text,flags=re.I)
    text=re.sub(r'\s*\([^)]{25,}\)', '', text)
    text=re.sub(r'\b(?:everyone loves|we are excited to|we are proud to)\b.*$', '', text, flags=re.I).strip()
    return text


def _study_relevance(name, pages, facts, business_summary, distinctive_summary):
    """Describe the research fit from this site's observed information structure."""
    generic={'about','about us','home','homepage','contact','welcome','l a g o o n'}
    topics=[]
    for page in pages:
        if page.get('type') in {'home_or_other','about'}:
            continue
        title=re.sub(re.escape(name), '', page['title'] or '', flags=re.I).strip(' -|—')
        title=title.split('|',1)[0].strip(' -|—')
        if title and title.casefold() not in generic and title.casefold() not in [x.casefold() for x in topics]:
            topics.append(title)
    topics=[x for x in topics if len(x)<55][:5]
    # The topic list is derived from the actual page titles/headings and avoids
    # assigning a canned business-type narrative (retail, hospitality, etc.).
    # Different information architectures therefore produce different copy.
    if topics:
        topic_text=', '.join(x.lower() for x in topics[:-1])+(' and ' if len(topics)>1 else '')+(topics[-1].lower() if topics else '')
        return (f'The site gives visitors separate pages for {topic_text}. That structure makes {name} relevant to a study of whether AI-powered search connects those public details when describing the business.')
    return (f'Your public information pairs {business_summary.rstrip(".")} with {distinctive_summary[0].lower()+distinctive_summary[1:].rstrip(".")}. '
            'That gives the study a concrete case for examining whether AI-powered search represents both a business’s main offering and the details that distinguish its service.')


def observe(url, html, stamp, name=None, truncated=False, additional_pages=(), failed_pages=()):
    """Build attributed company claims separately from supported GEO findings.

    One authoritative same-site source may support a claim. Cross-page
    corroboration is recorded when present, but is never required by itself.
    The analyzer does not infer deficiencies from missing sampled content.
    """
    raw_pages=[(url,html,truncated),*additional_pages]
    pages=[_page_profile(page_url,source,cut) for page_url,source,cut in raw_pages]
    return observe_profiles(pages, stamp, name=name, failed_pages=failed_pages)


def observe_profiles(pages, stamp, name=None, failed_pages=()):
    """Use previously captured extractor profiles without reconstructing HTML.

    The same evidence/claim producer serves both live and cached captures.
    Callers must validate the cache checksum and candidate identity first.
    """
    if not pages:
        raise ValueError('No captured first-party page profiles are available')
    source_urls=[page['url'] for page in pages]
    facts=[]
    paragraphs=[]
    for page_index,page in enumerate(pages):
        for paragraph_index,section in enumerate(page['sections']):
            text=section['text']
            if CODE_MARKERS.search(text):
                continue
            fact_id=f'p{page_index}_s{paragraph_index}'
            fact={'id':fact_id,'statement':text,'evidence_excerpt':text[:900],
                  'source_content':text,
                  'source_urls':[page['url']],'source_title':page['title'],
                  'section_heading':section['heading'],'assertion_type':'FIRST_PARTY_PAGE_CLAIM',
                  'attribution':'COMPANY_STATED'}
            facts.append(fact)
            paragraphs.append((page,section,fact))
        for index,item in enumerate(page['structured_facts']):
            fact={'id':f'p{page_index}_jsonld_{index}','statement':f"Structured {item['field']} value: {item['value']}",
                  'evidence_excerpt':item['value'],'source_content':item['value'],'source_urls':[page['url']],
                  'source_title':page['title'],'assertion_type':'STRUCTURED_FIRST_PARTY_DATA',
                  'attribution':'PUBLISHED_STRUCTURED_DATA','field':item['field']}
            facts.append(fact)

    # Prefer a page section that explicitly introduces the organization or an
    # offering. Rank evidence by source context and detail, not by a required
    # page count or a business-specific output template.
    def section_score(item, purpose):
        page,section,fact=item
        text=section['text']
        heading=section['heading'] or ''
        tokens=_content_terms(text)
        score=min(len(tokens),35)
        if page['type']=='about': score+=12
        if page is pages[0]: score+=4
        if name and name.casefold() in text.casefold(): score+=8
        if _first_party(text,name): score+=3
        if re.search(r'\b(?:services?|products?|offerings?|capabilities|work|practice|treatments?|menu|what we offer)\b',heading+' '+text,re.I): score+=5
        if purpose=='strength':
            if re.search(r'\b(?:certified|trained|speciali[sz]e|custom|handmade|award|years|research|testing|in-house|locally|one-of-a-kind|tailored|proprietary)\b',text,re.I): score+=8
            score+=min(text.count(',')+text.count(';'),6)
        return score

    if not paragraphs:
        raise ValueError('No visible first-party business claims were available in the permitted page sample')
    sentence_items=[(page,section,fact,sentence) for page,section,fact in paragraphs
                    for sentence in _sentences(section['text']) if len(_content_terms(sentence))>=7]
    description_candidates=[item for item in sentence_items if _first_party(item[3],name)]
    if not description_candidates:
        description_candidates=sentence_items
    def description_score(item):
        page,section,fact,sentence=item
        score=min(len(_content_terms(sentence)),24)
        if page['type']=='about': score+=2
        if name and name.casefold() in sentence.casefold(): score+=8
        if re.search(r'\b(?:is|are|offers?|provides?|speciali[sz]es?|serves?|focus(?:es)? on|operates?)\b',sentence,re.I): score+=5
        if re.search(r'\b(?:focus(?:es)? on|speciali[sz]es? in|provides?|offers?)\b.{0,80}\b(?:products?|services?|nutrition|design|care|lodging|meals?|consulting|treatment|classes|experiences?)\b',sentence,re.I): score+=18
        if re.search(r'\b(?:is|are)\s+(?:more than\s+)?(?:a|an|the)\s+[^,.;]{2,70}',sentence,re.I): score+=12
        if re.search(r'\b(?:store|studio|clinic|hotel|restaurant|practice|shop|products?|services?|guests?|clients?|customers?)\b',sentence,re.I): score+=4
        if re.search(r'\b(?:founder|lifelong passion|background and motivations|before launching|my career|my personal story)\b',sentence,re.I): score-=12
        return score
    description_item=max(description_candidates,key=description_score)
    description_page,description_section,description_fact,description_sentence=description_item
    description_sentence=description_sentence[:650].rstrip()
    description=f"{name or 'The company'}'s {description_page['title'] or 'website'} states: “{description_sentence}”"

    strength_candidates=[item for item in sentence_items if _first_party(item[3],name)] or sentence_items
    def strength_score(item):
        page,section,fact,sentence=item
        score=min(len(_content_terms(sentence)),24)
        if page['type']=='about': score+=3
        context=section['text']
        score+=min(5,len(re.findall(r'\b(?:certified|trained|custom|tailor\w*|testing|in-house|one-of-a-kind|proprietary|zero-waste|repurposed)\b',context,re.I)))*10
        score+=min(4,len(re.findall(r'\b(?:research|track|review|delivery|workshop|personalized|loyalty|in-store|handmade|speciali[sz]e|award|team|staff|program|service|recommendations?|curate|create)\b',context,re.I)))*3
        if re.search(r'\b(?:\$\s?\d+|\d+/hr|fee|deposit|non-refundable)\b',sentence,re.I): score-=20
        if page['type']=='services': score+=6
        return score
    strength_item=max(strength_candidates,key=strength_score)
    strength_page,strength_section,strength_fact,strength_sentence=strength_item
    strength_sentence=strength_sentence[:650].rstrip()
    strength=_distinctive_summary(name or 'The company',strength_sentence,strength_section['heading'],strength_section['text'])

    # Identical visible link labels pointing to different destinations can
    # support a bounded navigation question. It is phrased conditionally and
    # never treats complementary destinations as an established defect.
    recommendations=[]
    relationships=[]
    linked=[]
    for page in pages:
        page_fact=' '.join(f['statement'] for f in facts if page['url'] in f['source_urls']).casefold()
        if not re.search(r'\b(?:recordings?|archives?|readings?|events?)\b',page_fact):
            continue
        for link in page['links']:
            if re.search(r'\b(?:youtube|recording|archive|stream)\b',link['label']+' '+link['url'],re.I):
                linked.append((page,link,page_fact))
    by_label={}
    for page,link,page_fact in linked:
        by_label.setdefault(re.sub(r'\W+',' ',link['label'].casefold()).strip(),[]).append((page,link,page_fact))
    for label,items in by_label.items():
        destinations={item[1]['url'] for item in items}
        page_urls={item[0]['url'] for item in items}
        if label and len(destinations)>1 and len(page_urls)>1 and len(re.findall(r'\b(?:recordings?|archives?|readings?)\b',' '.join(x[2] for x in items)))>=2:
            first=items[0]
            second=next(x for x in items[1:] if x[1]['url']!=first[1]['url'])
            excerpt=(f'{first[0]["title"]}: {first[2][:700]} Link label “{first[1]["label"]}” points to {first[1]["url"]}. '
                     f'{second[0]["title"]}: {second[2][:700]} Link label “{second[1]["label"]}” points to {second[1]["url"]}.')
            problem=(f'The reviewed pages use the label “{label}” for recording or archive links, but the links point to different destinations; '
                     'the sampled page text does not explain whether the collections are complementary.')
            suggestion=('If these are complementary collections, label each destination with the recordings it contains so visitors can choose the relevant archive.')
            rec={'title':'Clarify the scope of linked recording collections','problem':problem,
                 'suggestion':suggestion,'evidence_excerpt':excerpt,
                 'source_urls':[first[0]['url'],second[0]['url']],'observed_at':stamp}
            recommendations.append(rec)
            relationships.append({'kind':'SAME_LABEL_DIFFERENT_DESTINATIONS','label':label,
                'source_urls':[first[0]['url'],second[0]['url']],
                'destinations':[first[1]['url'],second[1]['url']],
                'interpretation':'The destinations may be complementary; their relationship is not established by the sampled text.'})
            break

    geo_status='SUPPORTED' if recommendations else 'NOT_ESTABLISHED'
    limitations=[]
    failed_urls=list(failed_pages)
    if any(page['truncated'] for page in pages): limitations.append('One or more pages were truncated at the configured byte limit.')
    if len(pages)<2:
        limitations.append('Only one permitted first-party page was available for this sample.')
    if failed_urls:
        limitations.append('Some linked same-site pages could not be accessed; no attempt was made to bypass their restrictions: '+', '.join(failed_urls))
    business_summary=_recipient_summary(name or 'The company',description_sentence,description_section['heading'],'description')
    distinctive_summary=strength
    study_relevance=_study_relevance(name or 'The company',pages,facts,business_summary,distinctive_summary)
    observation_summary=''
    if recommendations:
        observation_summary=('The homepage points missed readings to YouTube streams; the events page uses the same archive label for a separate posts page. '
            'These destinations may be complementary, so briefly describing each could help readers find the right recording.')
    assessment={'personalization_evidence':'SUFFICIENT','geo_observation':geo_status,
                'invitation':'ELIGIBLE_FOR_REVIEW','human_review_required':True,
                'limitations':limitations,'interpretation':'Business claims below are attributed to the company’s own published pages; they are not independently verified.'}
    description_fact=dict(description_fact)
    description_fact['statement']=business_summary
    description_fact['assertion_type']='ATTRIBUTED_COMPANY_DESCRIPTION'
    # The internal evidence profile retains the full original paragraph even
    # when recipient copy summarizes one sentence from it.
    description_fact['evidence_excerpt']=description_section['text']
    strength_fact=dict(strength_fact)
    strength_fact['statement']=distinctive_summary
    strength_fact['assertion_type']='ATTRIBUTED_COMPANY_STRENGTH'
    # Retain the exact source paragraph when the concise summary synthesizes
    # more than one clause from it.
    strength_fact['evidence_excerpt']=strength_section['text']
    # Keep the actual source excerpt separately from the system's attributed
    # phrasing so reviewers can inspect exactly what supports the claim.
    profile_facts=[]
    for fact in facts:
        if fact['id'] in {description_fact['id'],strength_fact['id']}:
            profile_facts.append(description_fact if fact['id']==description_fact['id'] else strength_fact)
        else:
            profile_facts.append(fact)
    result={'analysis_version':'structured-evidence-v2','data_classification':'OBSERVED',
            'business_description':business_summary,'company_strength':distinctive_summary,
            'recipient_copy':{'business_summary':business_summary,'distinctive_detail':distinctive_summary,
                             'research_relevance':study_relevance,'supported_observation':observation_summary,
                             'support':{'business_summary':[description_fact['id']],
                                        'distinctive_detail':[strength_fact['id']],
                                        'research_relevance':[f['id'] for f in facts if f.get('source_urls') and f['source_urls'][0] in source_urls]}},
            'source_urls':source_urls,'retrieved_at':stamp,'recommendations':recommendations,
            'assessment':assessment,
            'claim_support':{'business_description':[description_fact['id']],
                             'company_strength':[strength_fact['id']]},
            'evidence_profile':{'pages':[{**{k:page[k] for k in ('url','type','title','headings','truncated')},
                                          'links':page['links'][:100],
                                          **{k:page[k] for k in ('retrieved_at','content_hash','source_evidence_id') if page.get(k)}}
                                         for page in pages],
                                'facts':profile_facts,'relationships':relationships},
            'evidence':{'description_quote':description_fact['evidence_excerpt'],
                        'visible_text_excerpt':' '.join(page['visible_text'] for page in pages)[:5000],
                        'sampled_urls':source_urls,'content_sample_truncated':any(page['truncated'] for page in pages),
                        'strength_support_excerpt':strength_fact['evidence_excerpt'],
                        'limitations':limitations}}
    from .quality import quality_decisions
    decisions=quality_decisions(result)
    result['review_decisions']=decisions
    result['assessment']['EVIDENCE_VALID']=decisions['EVIDENCE_VALID']['passed']
    result['assessment']['OUTREACH_REVIEW_READY']=decisions['OUTREACH_REVIEW_READY']['passed']
    return result


def _sample(fetcher, website, stamp, name=None):
    first = fetcher(website)
    home_url, home_html = first[:2]
    if not same_site(website, home_url):
        raise ValueError('Homepage redirect leaves the verified website')
    pages = [(home_url, home_html, first[2] if len(first) > 2 else False)]
    failed_pages=[]
    for target in _evidence_urls(home_url, home_html):
        try:
            fetched = fetcher(target)
            page_url, page_html = fetched[:2]
            if same_site(website, page_url):
                pages.append((page_url, page_html, fetched[2] if len(fetched) > 2 else False))
        except Exception as exc:
            # A failed secondary page does not erase usable homepage evidence.
            if isinstance(exc, httpx.TransportError) or isinstance(exc, httpx.HTTPStatusError):
                failed_pages.append(target)
                continue
            if isinstance(exc, ValueError) and ('robots' in str(exc).lower() or 'HTML' in str(exc)):
                failed_pages.append(target)
                continue
            raise
        if len(pages) >= MAX_PAGES_PER_CANDIDATE:
            break
    return observe(home_url, home_html, stamp, name=name, additional_pages=pages[1:], failed_pages=failed_pages)


def _domain_key(value):
    return (value or '').lower().removeprefix('www.').strip()


def _failure_kind(exc):
    if isinstance(exc, httpx.TimeoutException):
        return 'TRANSIENT_HTTP'
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return 'TRANSIENT_HTTP' if code in {408, 425, 429} or code >= 500 else 'PERMANENT_EVIDENCE'
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError, socket.gaierror)):
        return 'TRANSIENT_HTTP'
    if isinstance(exc, ValueError):
        return 'PERMANENT_EVIDENCE'
    return 'TRANSIENT_HTTP'


def _retry_time(kind, attempts, stamp):
    if kind == 'PERMANENT_EVIDENCE' or attempts >= 5:
        delay = timedelta(days=30)
    else:
        delay = timedelta(minutes=min(1440, 15 * (2 ** max(0, attempts - 1))))
    return (datetime.fromisoformat(stamp) + delay).isoformat()


def _env_flag(name, default=False):
    value = __import__('os').environ.get(name)
    return default if value is None else value.strip().casefold() in {'1', 'true', 'yes', 'on'}


def _money_env(name):
    from decimal import Decimal, InvalidOperation
    raw = __import__('os').environ.get(name, '0')
    try:
        amount = Decimal(raw)
    except (InvalidOperation, TypeError):
        raise ValueError(f'{name} must be a finite non-negative decimal')
    if not amount.is_finite() or amount < 0:
        raise ValueError(f'{name} must be a finite non-negative decimal')
    return amount


def _safe_json(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _eligible_query(stamp, selected_ids=(), retry_candidate_ids=()):
    no_prior_invitation = ~exists(select(invitations.c.id).where(invitations.c.candidate_id == candidates.c.id))
    personalization_state = candidates.c.personalization_status != 'READY'
    if selected_ids:
        legacy_selected = and_(candidates.c.id.in_(selected_ids), or_(
            candidates.c.personalization_analysis_version != 'semantic-openai-v1',
            candidates.c.personalization_quality_status != 'PASS'))
        personalization_state = or_(personalization_state, legacy_selected)
    if retry_candidate_ids:
        # The batch selector has already admitted these exact IDs from the
        # immutable admin-approved manifest. Permit legacy READY rows within
        # that selected slice; only semantic PASS is complete for the cohort.
        legacy_batch_selected = and_(candidates.c.id.in_(retry_candidate_ids), or_(
            candidates.c.personalization_analysis_version.is_(None),
            candidates.c.personalization_analysis_version != 'semantic-openai-v1',
            candidates.c.personalization_quality_status != 'PASS'))
        personalization_state = or_(personalization_state, legacy_batch_selected)
    retry_state=or_(candidates.c.personalization_retry_after.is_(None), candidates.c.personalization_retry_after <= stamp)
    if selected_ids: retry_state=or_(retry_state,candidates.c.id.in_(selected_ids))
    if retry_candidate_ids:
        # The durable batch selector may admit a future-backoff candidate only
        # when its prior provider response is known-cost and replayable. Keep
        # the inner selector aligned, but independently fail closed on any
        # unresolved spend reservation before claiming a lease.
        saved_response = exists(select(personalization_attempts.c.id).where(
            personalization_attempts.c.candidate_id == candidates.c.id,
            personalization_attempts.c.status.in_(['COMPLETED', 'VALIDATION_FAILED']),
            personalization_attempts.c.actual_cost_usd.is_not(None),
            personalization_attempts.c.actual_cost_usd != '0',
            personalization_attempts.c.provider_response.is_not(None)))
        no_unresolved_attempt = ~exists(select(personalization_attempts.c.id).where(
            personalization_attempts.c.candidate_id == candidates.c.id,
            personalization_attempts.c.status.in_(['UNKNOWN_SPEND', 'RESERVED'])))
        replayable_outcome = and_(
            candidates.c.id.in_(retry_candidate_ids),
            or_(candidates.c.personalization_outcome.in_([
                    'PROVIDER_JSON_SCHEMA_VALIDATION', 'PROVIDER_EVIDENCE_REFERENCE_VALIDATION',
                    'PROVIDER_IDENTITY_VALIDATION', 'PROVIDER_SEMANTIC_CONTRACT_VALIDATION',
                    'PROVIDER_STRUCTURED_JSON_PARSE', 'SEMANTIC_QUALITY_REJECTED']),
                and_(candidates.c.personalization_outcome == 'PERMANENT_EVIDENCE',
                     candidates.c.personalization_error.not_in([
                         'No V1 recommendation is supported by this bounded same-site sample; preserve existing data and defer',
                         'HTTP 301', 'HTTP 308', 'HTTPStatusError', 'Website redirect limit exceeded',
                         'Website robots policy could not be checked',
                         'A grounded business description cannot yet be established from the sampled same-site pages',
                         'No visible first-party business claims were available in the permitted page sample']))),
            saved_response, no_unresolved_attempt,
            replay_allowed_for_contract(candidates.c.id, candidates.c.personalization_outcome))
        no_attempt = ~exists(select(personalization_attempts.c.id).where(
            personalization_attempts.c.candidate_id == candidates.c.id))
        legacy_no_geo = and_(
            candidates.c.personalization_error ==
                'No V1 recommendation is supported by this bounded same-site sample; preserve existing data and defer',
            candidates.c.personalization_outcome == 'PERMANENT_EVIDENCE',
            no_attempt)
        redirect_or_http_repair = and_(
            candidates.c.personalization_error.in_([
                'HTTP 301', 'HTTP 308', 'HTTPStatusError', 'Website redirect limit exceeded',
                'Website robots policy could not be checked']),
            candidates.c.personalization_outcome.in_(['PERMANENT_EVIDENCE', 'TRANSIENT_HTTP']),
            no_attempt)
        evidence_refresh_no_attempt = ~exists(select(personalization_attempts.c.id).where(
            personalization_attempts.c.candidate_id == candidates.c.id))
        evidence_refresh_not_used = ~exists(select(events.c.id).where(
            events.c.entity_id == candidates.c.id,
            events.c.event == 'PERSONALIZATION_FIRST_PARTY_EVIDENCE_REFRESH_V1'))
        first_party_evidence_refresh = and_(
            candidates.c.id.in_(retry_candidate_ids),
            candidates.c.personalization_outcome == 'PERMANENT_EVIDENCE',
            candidates.c.personalization_error.in_([
                'A grounded business description cannot yet be established from the sampled same-site pages',
                'No visible first-party business claims were available in the permitted page sample']),
            evidence_refresh_no_attempt, evidence_refresh_not_used)
        replayable_local_rejection = and_(
            or_(and_(candidates.c.personalization_outcome == 'KNOWN_RESPONSE_REPLAY_REJECTED',
                     candidates.c.personalization_error.in_([
                         'Model abstained while returning GEO hypotheses; inconsistent response',
                         'Saved response does not contain the raw provider response',
                         'Outreach review quality failed: SOURCE_TEXT_COPIED_TO_RECIPIENT_COPY'])),
                and_(candidates.c.personalization_outcome.in_([
                    'KNOWN_RESPONSE_REPLAY_REJECTED',
                    'KNOWN_RESPONSE_REPLAY_REJECTED_AFTER_CLASSIFIER_REPAIR',
                    'KNOWN_RESPONSE_REPLAY_REJECTED_AFTER_CLASSIFIER_REPAIR_V2',
                    'KNOWN_RESPONSE_REPLAY_REJECTED_AFTER_CLASSIFIER_REPAIR_V3']),
                     candidates.c.personalization_error == 'No concise, cited first-party service statement is available'),
                and_(candidates.c.personalization_outcome == 'KNOWN_RESPONSE_REPLAY_REJECTED',
                     candidates.c.personalization_error ==
                         'No model-cited, first-party target-business description is available')),
            saved_response, no_unresolved_attempt,
            replay_allowed_for_contract(candidates.c.id, candidates.c.personalization_outcome))
        retry_state=or_(retry_state,replayable_outcome,legacy_no_geo,redirect_or_http_repair,
                        first_party_evidence_refresh,
                        replayable_local_rejection)
    return and_(candidates.c.kind.in_(['company', 'hotel']), candidates.c.fixture.is_(False),
        or_(candidates.c.personalization_outcome.is_(None),
            candidates.c.personalization_outcome != 'NO_AUTOMATIC_RETRY'),
        candidates.c.website_status == 'VERIFIED', candidates.c.qualification.in_(['ELIGIBLE', 'QUALIFIED']),
        candidates.c.suppressed.is_(False), personalization_state, retry_state,
        or_(candidates.c.personalization_lease_until.is_(None), candidates.c.personalization_lease_until < stamp),
        no_prior_invitation)


def _analysis_fingerprint(selection):
    import hashlib
    return hashlib.sha256(_safe_json(selection.to_input()).encode('utf-8')).hexdigest()


def backfill(db, limit=10, fetcher=fetch_page, *, concurrency=1, time_budget_seconds=240,
             authorized_candidate_ids=None, selected_candidate_ids=None, retry_candidate_ids=None,
             backend_factory=None, token_count=None, approved_cumulative_budget_usd=None,
             approved_candidate_budget_usd=None, approved_cohort_budget_usd=None,
             approved_cohort_candidate_ids=None):
    """Run the real semantic pipeline; paid execution is disabled unless explicitly configured.

    Candidate websites are fetched and attributed before an OpenAI request is possible.
    Each candidate is committed independently. One completed response is retained by
    evidence fingerprint and replayed on later runs, preventing duplicate model spend.
    """
    import json, os
    from decimal import Decimal
    from .semantic_openai import (MODEL, MAX_ESTIMATED_USD, OFFLINE_REPLAY_CONTRACT_ID,
        OpenAIResponsesBackend,
        build_preflight, select_evidence, build_automated_personalization,
        OpenAIRequestError, PreflightError, attempt_failure_accounting,
        replay_saved_provider_attempt)
    from .quality import quality_decisions, quality_reasons
    from .template import validate

    if not 1 <= limit <= MAX_PILOT_CANDIDATES:
        raise ValueError('The authorized personalization pilot is limited to 1–5 candidates per run')
    if concurrency != 1:
        raise ValueError('Paid semantic requests must run sequentially; concurrency must be 1')
    if not 1 <= time_budget_seconds <= 300:
        raise ValueError('AI personalization runtime budget must be between 1 and 300 seconds')
    if not _env_flag('OUTREACH_AI_PERSONALIZATION_ENABLED', False):
        return {'status': 'DISABLED', 'disabled': True, 'attempted': 0, 'completed': 0,
                'incomplete': 0, 'skipped_existing_attempt': 0, 'remaining_backlog': None,
                'reason': 'OUTREACH_AI_PERSONALIZATION_ENABLED is not true; no model calls were made.'}
    authorized_ids = tuple(dict.fromkeys(str(x).strip() for x in (authorized_candidate_ids or ()) if str(x).strip()))
    selected_ids = tuple(dict.fromkeys(str(x).strip() for x in (selected_candidate_ids or ()) if str(x).strip()))
    retry_ids = tuple(dict.fromkeys(str(x).strip() for x in (retry_candidate_ids or ()) if str(x).strip()))
    if not authorized_ids:
        return {'status':'BLOCKED_CONFIGURATION','disabled':True,'attempted':0,'completed':0,
                'reason':'No explicit authorized candidate cohort is configured; automatic database-wide selection is disabled.'}
    if len(authorized_ids)>MAX_PILOT_CANDIDATES or len(authorized_ids)>limit:
        return {'status':'BLOCKED_CONFIGURATION','disabled':True,'attempted':0,'completed':0,
                'reason':'Authorized candidate cohort exceeds the five-company pilot/run limit.'}
    if not set(selected_ids).issubset(set(authorized_ids)):
        return {'status':'BLOCKED_CONFIGURATION','disabled':True,'attempted':0,'completed':0,
                'reason':'Historical reprocessing IDs must be within the exact authorized candidate cohort.'}
    if not set(retry_ids).issubset(set(authorized_ids)):
        return {'status':'BLOCKED_CONFIGURATION','disabled':True,'attempted':0,'completed':0,
                'reason':'Retry recovery IDs must be within the exact authorized candidate cohort.'}
    operation_id=os.environ.get('OUTREACH_AI_PERSONALIZATION_OPERATION_ID','initial').strip()
    if not operation_id or len(operation_id)>128:
        raise ValueError('AI personalization operation ID must contain 1–128 characters')
    if len(selected_ids)>limit:
        raise ValueError('Selected reprocessing IDs cannot exceed the per-run candidate limit')
    if selected_ids and not _env_flag('OUTREACH_AI_PERSONALIZATION_REPROCESS_ENABLED', False):
        return {'status': 'BLOCKED_CONFIGURATION', 'disabled': True, 'attempted': 0,
                'reason': 'Explicit historical READY reprocessing is disabled.'}
    if os.environ.get('OUTREACH_AI_PERSONALIZATION_MODEL', MODEL) != MODEL:
        raise ValueError('Configured model is not on the exact AI personalization allowlist')
    run_budget = _money_env('OUTREACH_AI_PERSONALIZATION_RUN_BUDGET_USD')
    cumulative_budget = _money_env('OUTREACH_AI_PERSONALIZATION_CUMULATIVE_BUDGET_USD')
    per_candidate_cap = _money_env('OUTREACH_AI_PERSONALIZATION_MAX_COST_PER_CANDIDATE_USD')
    approved_cumulative=(Decimal(str(approved_cumulative_budget_usd)) if approved_cumulative_budget_usd is not None else None)
    approved_candidate=(Decimal(str(approved_candidate_budget_usd)) if approved_candidate_budget_usd is not None else None)
    approved_cohort=(Decimal(str(approved_cohort_budget_usd)) if approved_cohort_budget_usd is not None else None)
    approved_cohort_ids=tuple(dict.fromkeys(str(x).strip() for x in (approved_cohort_candidate_ids or ()) if str(x).strip()))
    if not (Decimal('0') < run_budget <= Decimal(MAX_PILOT_SPEND_USD) and
            Decimal('0') < cumulative_budget <= Decimal(MAX_PILOT_SPEND_USD) and
            Decimal('0') < per_candidate_cap <= Decimal(str(MAX_ESTIMATED_USD))):
        return {'status': 'BLOCKED_CONFIGURATION', 'disabled': True, 'attempted': 0,
                'reason': 'Run and cumulative budgets must be positive and no greater than the authorized $0.25 pilot ceiling; per-candidate cap must remain within $0.05.'}
    if approved_cumulative is not None:
        # Larger lifetime budgets are accepted only from a separately persisted
        # administrator-approved grant; the per-run and per-candidate caps stay fixed.
        if not Decimal('0') < approved_cumulative <= Decimal('5.00'):
            return {'status':'BLOCKED_CONFIGURATION','disabled':True,'attempted':0,
                'reason':'Approved cumulative authorization must be positive and no more than $5.00.'}
        # A persisted batch grant may raise only the cumulative ceiling to its
        # approved value. Keep the configured run and per-candidate safeguards;
        # the batch runner invokes at most five candidates per run.
        cumulative_budget=approved_cumulative
    if approved_candidate is not None:
        # A one-candidate grant is an additional, exact-scope authorization.
        # Keep the configured lifetime ceiling in force for the whole ledger;
        # enforce the grant separately against this candidate's ledger history.
        if (len(authorized_ids) != 1 or not Decimal('0') < approved_candidate <= Decimal('0.05')
                or approved_cumulative is not None):
            return {'status':'BLOCKED_CONFIGURATION','disabled':True,'attempted':0,
                'reason':'Approved candidate authorization must be a single-candidate limit no greater than $0.05.'}
        run_budget=min(run_budget,approved_candidate)
    if approved_cohort is not None:
        if (not Decimal('0') < approved_cohort <= Decimal('2.50') or len(approved_cohort_ids) != 50
                or not set(authorized_ids).issubset(set(approved_cohort_ids))
                or approved_candidate is not None):
            return {'status':'BLOCKED_CONFIGURATION','disabled':True,'attempted':0,
                'reason':'Approved replacement-cohort authorization must bind the exact 50-candidate scope and remain within $2.50.'}
    configured_limit = int(os.environ.get('OUTREACH_AI_PERSONALIZATION_MAX_CANDIDATES_PER_RUN', str(limit)))
    if not 1 <= configured_limit <= MAX_PILOT_CANDIDATES:
        return {'status':'BLOCKED_CONFIGURATION','disabled':True,'attempted':0,'completed':0,
                'reason':'Configured personalization run limit must remain within the five-company pilot ceiling.'}
    limit = min(limit, configured_limit)
    if backend_factory is None:
        backend_factory = OpenAIResponsesBackend
    # Credential presence is checked before claiming candidates or fetching pages.
    try:
        backend = backend_factory()
    except Exception as exc:
        from .semantic_openai import CredentialUnavailable
        if isinstance(exc, CredentialUnavailable):
            return {'status':'BLOCKED_CONFIGURATION','disabled':True,'attempted':0,
                    'reason':'OpenAI credential is unavailable to the runtime; no candidates were claimed and no model call was made.'}
        raise

    stamp = now()
    run_id = 'personalization-run-' + uuid.uuid4().hex
    run_started = time.monotonic()
    deadline = run_started + time_budget_seconds
    lease_token = uuid.uuid4().hex
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    eligible = _eligible_query(stamp, selected_ids, retry_ids)
    backlog = and_(candidates.c.kind.in_(['company', 'hotel']), candidates.c.fixture.is_(False),
        candidates.c.website_status == 'VERIFIED', candidates.c.qualification.in_(['ELIGIBLE', 'QUALIFIED']),
        candidates.c.suppressed.is_(False), candidates.c.personalization_status != 'READY',
        ~exists(select(invitations.c.id).where(invitations.c.candidate_id == candidates.c.id)))
    claimed=[]
    domains=set()
    domain_cooldown=(datetime.now(timezone.utc)-timedelta(minutes=15)).isoformat()
    with db.transaction(serialized=True) as c:
        c.execute(personalization_runs.insert().values(id=run_id, started_at=stamp, status='RUNNING',
            candidate_limit=limit, run_budget_usd=str(run_budget), cumulative_budget_usd=str(cumulative_budget),
            reserved_usd='0', actual_cost_usd='0', detail='{}'))
        recent=set(c.execute(select(candidates.c.domain).select_from(
            events.join(candidates,events.c.entity_id==candidates.c.id)).where(
            events.c.event.in_(['PERSONALIZATION_READY','PERSONALIZATION_INCOMPLETE']),
            events.c.created_at>=domain_cooldown)).scalars())
        recent={_domain_key(x) for x in recent if x}
        q=select(candidates).where(eligible, candidates.c.id.in_(authorized_ids)).order_by(
            candidates.c.personalization_at.asc().nulls_first(), candidates.c.id).limit(min(1000,limit*20))
        if db.postgres: q=q.with_for_update(skip_locked=True)
        for item in c.execute(q).mappings():
            row=dict(item); domain=_domain_key(row.get('domain'))
            if domain in domains or (domain in recent and row['id'] not in selected_ids and row['id'] not in retry_ids): continue
            updated=c.execute(candidates.update().where(candidates.c.id==row['id'],eligible).values(
                personalization_lease_token=lease_token,personalization_lease_until=lease_until))
            if updated.rowcount==1:
                claimed.append(row);domains.add(domain)
            if len(claimed)>=limit:break

    result={'status':'COMPLETED','run_id':run_id,'model':MODEL,'operation_id':operation_id,'attempted':0,'completed':0,'incomplete':0,
        'replayed':0,'skipped_existing_attempt':0,'budget_stopped':False,'elapsed_seconds':0,
        'remaining_backlog':0,'failure_categories':{},'failure_reasons':{}}

    def commit_outcome(row, personalization, quality_status, quality_errors, fp, attempt, outcome, error=None,
                       *, rejected_replay=False):
        at=now(); passed=quality_status=='PASS'
        status='READY' if passed else 'INCOMPLETE'
        with db.transaction(serialized=True) as c:
            current=c.execute(select(candidates).where(candidates.c.id==row['id'],
                candidates.c.personalization_lease_token==lease_token).with_for_update()).mappings().first()
            if not current: return False
            old_version=int(current.get('personalization_version') or 0)
            old_analysis=current.get('personalization_analysis_version')
            if old_version==0:
                old_value=current.get('personalization') or '{}'
                if not isinstance(old_value,str): old_value=_safe_json(old_value)
                try: old_obj=json.loads(old_value)
                except Exception: old_obj={}
                c.execute(personalization_versions.insert().values(id=uuid.uuid4().hex,candidate_id=row['id'],version=0,
                    analysis_version='LEGACY_SNAPSHOT',evidence_fingerprint='legacy:'+str(current.get('personalization_at') or current.get('id')),
                    personalization=old_value,quality_status=current.get('personalization_quality_status') or 'UNVERIFIED',
                    quality_errors=current.get('personalization_quality_errors') or '[]',provider='legacy',model=None,request_id=None,
                    usage='{}',estimated_cost_usd='0',created_at=at))
            new_version=old_version+1
            encoded=_safe_json(personalization)
            c.execute(personalization_versions.insert().values(id=uuid.uuid4().hex,candidate_id=row['id'],version=new_version,
                analysis_version='semantic-openai-v1',evidence_fingerprint=fp,personalization=encoded,
                quality_status=quality_status,quality_errors=_safe_json(quality_errors),
                provider='openai',model=(personalization.get('semantic_execution') or {}).get('model'),
                request_id=(personalization.get('semantic_execution') or {}).get('request_id'),
                usage=_safe_json((personalization.get('semantic_execution') or {}).get('usage') or {}),
                estimated_cost_usd=str((personalization.get('semantic_execution') or {}).get('estimated_cost_usd') or 0),created_at=at))
            values={'personalization':encoded,'personalization_status':status,'personalization_quality_status':quality_status,
                'personalization_quality_errors':_safe_json(quality_errors),'personalization_at':at,
                'personalization_error':None if passed else ','.join(quality_errors)[:300],
                'personalization_outcome':('SEMANTIC_READY' if passed else
                    'KNOWN_RESPONSE_REPLAY_REJECTED' if rejected_replay else 'SEMANTIC_QUALITY_REJECTED'),
                'personalization_retry_after':None if passed else _retry_time('PERMANENT_EVIDENCE',int(row.get('personalization_attempts') or 0)+1,at),
                'personalization_attempts':int(row.get('personalization_attempts') or 0)+1,
                'personalization_version':new_version,'personalization_analysis_version':'semantic-openai-v1',
                'personalization_fingerprint':fp,'description':personalization.get('business_description')}
            c.execute(candidates.update().where(candidates.c.id==row['id'],candidates.c.personalization_lease_token==lease_token)
                .values(**values,personalization_lease_token=None,personalization_lease_until=None))
            if attempt:
                c.execute(personalization_attempts.update().where(personalization_attempts.c.id==attempt['id']).values(
                    status='COMPLETED',actual_cost_usd=str((personalization.get('semantic_execution') or {}).get('estimated_cost_usd') or 0),
                    request_id=(personalization.get('semantic_execution') or {}).get('request_id'),
                    usage=_safe_json((personalization.get('semantic_execution') or {}).get('usage') or {}),
                    provider_response=attempt.get('provider_response'),finished_at=at))
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=row['id'],event='PERSONALIZATION_'+status,
                detail=_safe_json({'classification':values['personalization_outcome'],'quality_status':quality_status,
                    'quality_errors':quality_errors,'analysis_version':'semantic-openai-v1','fingerprint':fp,'operation_id':operation_id,
                    'source_urls':personalization.get('source_urls',[])}),created_at=at))
        return True

    def release(row, status='INCOMPLETE', reason='', retry_kind='TRANSIENT_HTTP', outcome='TRANSIENT_HTTP',
                replay_contract_id=None, evidence_fingerprint=None):
        at=now(); attempts=int(row.get('personalization_attempts') or 0)+1
        with db.transaction(serialized=True) as c:
            c.execute(candidates.update().where(candidates.c.id==row['id'],candidates.c.personalization_lease_token==lease_token)
                .values(personalization_status=status,personalization_at=at,personalization_error=reason[:300] or None,
                    personalization_outcome=outcome,personalization_attempts=attempts,
                    personalization_retry_after=_retry_time(retry_kind,attempts,at),
                    personalization_lease_token=None,personalization_lease_until=None))
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=row['id'],event='PERSONALIZATION_'+status,
                detail=_safe_json({'classification':outcome,'error':reason[:200]}),created_at=at))
            if replay_contract_id:
                c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=row['id'],
                    event='PERSONALIZATION_OFFLINE_REPLAY_REJECTED',
                    detail=_safe_json({'validator_contract': replay_contract_id,
                        'evidence_fingerprint': evidence_fingerprint, 'error': reason[:200]}),
                    created_at=at))

    for index,row in enumerate(claimed):
        replaying_saved_response=False
        if row['id'] not in authorized_ids:
            raise RuntimeError('Cohort isolation invariant failed before personalization')
        if time.monotonic()>=deadline:
            result['budget_stopped']=True
            break
        try:
            if (row.get('personalization_outcome') == 'PERMANENT_EVIDENCE'
                    and row.get('personalization_error') in {
                        'A grounded business description cannot yet be established from the sampled same-site pages',
                        'No visible first-party business claims were available in the permitted page sample'}):
                # One bounded evidence-only refresh is permitted for records
                # that failed before any provider attempt. The durable event
                # prevents repeatedly crawling the same unchanged candidate
                # after another insufficient-evidence result.
                with db.transaction(serialized=True) as c:
                    prior_attempt = c.execute(select(personalization_attempts.c.id).where(
                        personalization_attempts.c.candidate_id == row['id']).limit(1)).first()
                    prior_refresh = c.execute(select(events.c.id).where(
                        events.c.entity_id == row['id'],
                        events.c.event == 'PERSONALIZATION_FIRST_PARTY_EVIDENCE_REFRESH_V1').limit(1)).first()
                    if prior_attempt or prior_refresh:
                        c.execute(candidates.update().where(
                            candidates.c.id == row['id'],
                            candidates.c.personalization_lease_token == lease_token).values(
                                personalization_lease_token=None, personalization_lease_until=None))
                        result['skipped_existing_attempt'] += 1
                        continue
                    c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=row['id'],
                        event='PERSONALIZATION_FIRST_PARTY_EVIDENCE_REFRESH_V1',
                        detail=_safe_json({'extractor_version':'visible-blocks-v1',
                            'reason':'retry first-party page extraction after preserving verified visible div/list content',
                            'provider_attempts_before_refresh':0}), created_at=now()))
            observed=_sample(fetcher,row['website'],now(),row.get('name'))
            selection=select_evidence(observed,company_name=row['name'],website=row['website'])
            # Keep the existing synchronous worker endpoint within its bounded
            # request window; do not start a model call without enough time left.
            if deadline-time.monotonic()<21:
                result['budget_stopped']=True
                break
            evidence_input=selection.to_input()
            fp=_analysis_fingerprint(selection)
            preflight,_request=build_preflight(evidence_input,token_count=token_count)
            if Decimal(str(preflight.worst_case_cost_usd))>per_candidate_cap:
                raise PreflightError('Worst-case request cost exceeds configured per-candidate cap')
            with db.transaction(serialized=True) as c:
                previous=c.execute(select(personalization_attempts).where(
                    personalization_attempts.c.candidate_id==row['id'],
                    personalization_attempts.c.evidence_fingerprint==fp,
                    personalization_attempts.c.operation_id==operation_id).with_for_update()).mappings().first()
                prior_operation=(None if previous else c.execute(select(personalization_attempts).where(
                    personalization_attempts.c.candidate_id==row['id'],
                    personalization_attempts.c.operation_id==operation_id).order_by(
                        personalization_attempts.c.created_at.desc()).limit(1).with_for_update()).mappings().first())
                # The service-classification correction changes only the
                # deterministic label supplied to the model. Reuse a paid
                # response only when restoring those exact prior labels
                # recreates the archived request hash byte-for-byte.
                if (not previous and prior_operation
                        and prior_operation.get('status') in {'COMPLETED','VALIDATION_FAILED'}
                        and prior_operation.get('provider_response')
                        and prior_operation.get('actual_cost_usd')
                        and selection.legacy_classifications):
                    try:
                        archived=json.loads(prior_operation['provider_response'])
                        archived_hash=(archived.get('preflight') or {}).get('prompt_sha256')
                        if not archived_hash:
                            archived_hash=((archived.get('provider_response') or {}).get('preflight') or {}).get('prompt_sha256')
                        prior_preflight,_=build_preflight(
                            selection.prior_classification_input(),token_count=token_count)
                        if archived_hash and archived_hash==prior_preflight.prompt_sha256:
                            previous=prior_operation
                    except (TypeError,ValueError,PreflightError):
                        # Missing or malformed archived inputs are never
                        # treated as replay authorization.
                        pass
                if (previous and previous['status'] in {'COMPLETED','VALIDATION_FAILED'}
                        and previous.get('provider_response') and previous.get('actual_cost_usd')):
                    # Mark the replay before local validation starts. If replay
                    # rejects the saved response, it must leave the automatic
                    # retry set rather than being replayed on every batch tick.
                    replaying_saved_response=True
                    provider_result=replay_saved_provider_attempt(previous,evidence_input,preflight)
                    result['replayed']+=1;attempt=dict(previous)
                elif previous or prior_operation:
                    existing_attempt = previous or prior_operation
                    result['skipped_existing_attempt']+=1
                    at=now(); attempts=int(row.get('personalization_attempts') or 0)+1
                    c.execute(candidates.update().where(candidates.c.id==row['id'],candidates.c.personalization_lease_token==lease_token)
                        .values(personalization_status='INCOMPLETE',personalization_error='MODEL_ATTEMPT_NO_AUTOMATIC_RETRY',
                            personalization_outcome='NO_AUTOMATIC_RETRY',personalization_attempts=attempts,
                            personalization_retry_after=_retry_time('PERMANENT_EVIDENCE',attempts,at),
                            personalization_lease_token=None,personalization_lease_until=None))
                    c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=row['id'],event='PERSONALIZATION_NO_AUTOMATIC_RETRY',
                        detail=_safe_json({'attempt_status':existing_attempt['status'],'evidence_fingerprint':fp,
                            'saved_evidence_fingerprint':existing_attempt.get('evidence_fingerprint'),
                            'reason':'saved provider attempt belongs to different evidence or is not replayable'}),created_at=at))
                    continue
                else:
                    spent=Decimal('0')
                    candidate_spent=Decimal('0')
                    cohort_spent=Decimal('0')
                    rows=c.execute(select(personalization_attempts.c.status,personalization_attempts.c.reserved_usd,
                        personalization_attempts.c.actual_cost_usd,personalization_attempts.c.candidate_id).with_for_update()).all()
                    for item in rows:
                        row_cost=Decimal(str(item.actual_cost_usd or 0))
                        if item.status in {'RESERVED','UNKNOWN_SPEND'}:
                            row_cost=max(row_cost,Decimal(str(item.reserved_usd or 0)))
                        spent += row_cost
                        if approved_candidate is not None and item.candidate_id == row['id']:
                            candidate_spent += row_cost
                        if approved_cohort is not None and item.candidate_id in approved_cohort_ids:
                            cohort_spent += row_cost
                    run_reserved=Decimal(str(c.scalar(select(personalization_runs.c.reserved_usd).where(personalization_runs.c.id==run_id)) or 0))
                    request_reservation=Decimal(str(preflight.worst_case_cost_usd))
                    budget_failures=[]
                    if run_reserved+request_reservation>run_budget:
                        budget_failures.append('run_budget')
                    if spent+request_reservation>cumulative_budget:
                        budget_failures.append('cumulative_budget')
                    if approved_candidate is not None and candidate_spent+request_reservation>approved_candidate:
                        budget_failures.append('approved_candidate_budget')
                    if approved_cohort is not None and cohort_spent+request_reservation>approved_cohort:
                        budget_failures.append('approved_cohort_budget')
                    if budget_failures:
                        result['budget_stopped']=True
                        result['budget_stop_detail']={
                            'failed_comparisons':budget_failures,
                            'request_worst_case_usd':str(request_reservation),
                            'run_reserved_usd':str(run_reserved),'run_budget_usd':str(run_budget),
                            'ledger_spend_including_reservations_usd':str(spent),
                            'cumulative_budget_usd':str(cumulative_budget),
                            'candidate_spend_including_reservations_usd':str(candidate_spent),
                            'approved_candidate_budget_usd':str(approved_candidate) if approved_candidate is not None else None,
                            'cohort_spend_including_reservations_usd':str(cohort_spent),
                            'approved_cohort_budget_usd':str(approved_cohort) if approved_cohort is not None else None,
                        }
                        c.execute(candidates.update().where(candidates.c.id==row['id'],candidates.c.personalization_lease_token==lease_token)
                            .values(personalization_lease_token=None,personalization_lease_until=None))
                        continue
                    attempt_id=uuid.uuid4().hex
                    c.execute(personalization_attempts.insert().values(id=attempt_id,candidate_id=row['id'],run_id=run_id,
                        evidence_fingerprint=fp,operation_id=operation_id,model=MODEL,status='RESERVED',reserved_usd=str(preflight.worst_case_cost_usd),
                        actual_cost_usd='0',usage='{}',provider_response=None,created_at=now()))
                    c.execute(personalization_runs.update().where(personalization_runs.c.id==run_id).values(
                        reserved_usd=str(run_reserved+Decimal(str(preflight.worst_case_cost_usd)))))
                    attempt={'id':attempt_id,'provider_response':None}
                    provider_result=None
            if result['budget_stopped'] and provider_result is None:
                break
            if provider_result is None:
                # Exactly one network call; adapter has no retry layer. Preserve the
                # entire response for audit and later free replay.
                provider_result=backend.analyze_once(evidence_input,token_count=token_count)
                attempt['provider_response']=_safe_json(provider_result)
                with db.transaction(serialized=True) as c:
                    c.execute(personalization_attempts.update().where(personalization_attempts.c.id==attempt['id']).values(
                        status='COMPLETED',provider_response=attempt['provider_response'],request_id=provider_result.get('request_id'),
                        actual_cost_usd=str(provider_result.get('estimated_cost_usd') or 0),
                        usage=_safe_json(provider_result.get('usage') or {}),finished_at=now()))
            normalized,audit=build_automated_personalization(provider_result['structured_output'],selection,stamp=now())
            normalized['semantic_execution']={'provider':provider_result.get('provider'),'model':provider_result.get('model'),
                'request_id':provider_result.get('request_id'),'usage':provider_result.get('usage'),
                'estimated_cost_usd':provider_result.get('estimated_cost_usd'),
                'offline_replay':bool(provider_result.get('offline_replay')),
                'preflight':provider_result.get('preflight'),'model_claim_audit':audit,
                'structured_output':provider_result.get('structured_output'),
                'rejected_claims':provider_result.get('rejected_claims',[])}
            normalized['analysis_version']='structured-evidence-v2'
            normalized['pipeline_version']='semantic-openai-v1'
            try:
                validate(normalized,row['website'])
                decisions=quality_decisions(normalized,record=row,candidate_name=row['name'],website=row['website'])
                errors=[] if decisions['EVIDENCE_VALID']['passed'] and decisions['OUTREACH_REVIEW_READY']['passed'] else list(dict.fromkeys(
                    decisions['EVIDENCE_VALID']['reasons']+decisions['OUTREACH_REVIEW_READY']['reasons']))
            except (TypeError,ValueError) as exc:
                errors=quality_reasons(normalized) or [str(exc)[:180] or 'QUALITY_VALIDATION_FAILED']
            quality_status='PASS' if not errors else 'FAIL'
            if commit_outcome(row,normalized,quality_status,errors,fp,attempt,provider_result,
                              rejected_replay=replaying_saved_response and quality_status!='PASS'):
                result['attempted']+=1
                if quality_status=='PASS': result['completed']+=1
                else:
                    result['incomplete']+=1
                    result['failure_categories']['QUALITY_REJECTED']=result['failure_categories'].get('QUALITY_REJECTED',0)+1
                    for reason in errors: result['failure_reasons'][reason]=result['failure_reasons'].get(reason,0)+1
        except PreflightError as exc:
            with db.transaction() as c:
                c.execute(candidates.update().where(candidates.c.id==row['id'],candidates.c.personalization_lease_token==lease_token)
                    .values(personalization_lease_token=None,personalization_lease_until=None))
            result['status']='BLOCKED_CONFIGURATION';result['blocked_reason']=str(exc)[:200]
            result['failure_categories']['PREFLIGHT_BLOCKED']=result['failure_categories'].get('PREFLIGHT_BLOCKED',0)+1
            break
        except ValueError as exc:
            kind=_failure_kind(exc)
            outcome='KNOWN_RESPONSE_REPLAY_REJECTED' if replaying_saved_response else kind
            error=str(exc)
            if replaying_saved_response and error=='No concise, cited first-party service statement is available':
                # Keep the failure state stable. A future, identifiable
                # validator/evidence-contract change can reopen it once.
                outcome = 'KNOWN_RESPONSE_REPLAY_REJECTED_AFTER_CLASSIFIER_REPAIR_V3'
            elif (replaying_saved_response and
                  error == 'No model-cited, first-party target-business description is available'):
                outcome = 'KNOWN_RESPONSE_REPLAY_REJECTED_AFTER_DESCRIPTION_CITATION_REPAIR'
            release(row,'INCOMPLETE',error,retry_kind=kind,outcome=outcome,
                replay_contract_id=OFFLINE_REPLAY_CONTRACT_ID if replaying_saved_response else None,
                evidence_fingerprint=locals().get('fp') if replaying_saved_response else None)
            result['attempted']+=1;result['incomplete']+=1
            result['failure_categories'][kind]=result['failure_categories'].get(kind,0)+1
            result['failure_reasons'][str(exc)[:140]]=result['failure_reasons'].get(str(exc)[:140],0)+1
        except OpenAIRequestError as exc:
            # The request may have reached the provider. Keep the full reservation
            # when spend is unknown; otherwise record provider-reported usage/cost.
            with db.transaction(serialized=True) as c:
                attempt_row=c.execute(select(personalization_attempts).where(
                    personalization_attempts.c.candidate_id==row['id'],personalization_attempts.c.evidence_fingerprint==locals().get('fp',''),
                    personalization_attempts.c.operation_id==operation_id).with_for_update()).mappings().first()
                if attempt_row:
                    updates=attempt_failure_accounting(exc,attempt_row)
                    updates['finished_at']=now()
                    c.execute(personalization_attempts.update().where(personalization_attempts.c.id==attempt_row['id']).values(**updates))
            outcome=('KNOWN_RESPONSE_REPLAY_REJECTED' if replaying_saved_response
                     else 'PROVIDER_'+exc.failure_stage.upper())
            release(row,'INCOMPLETE','MODEL_REQUEST_'+exc.code,retry_kind='PERMANENT_EVIDENCE',outcome=outcome)
            result['status']='ATTENTION_REQUIRED'
            result['attempted']+=1;result['incomplete']+=1
            category='PROVIDER_VALIDATION_FAILED' if exc.actual_cost_usd is not None else 'UNKNOWN_SPEND'
            result['failure_categories'][category]=result['failure_categories'].get(category,0)+1
            result['failure_reasons']['MODEL_REQUEST_'+exc.code]=1
        except Exception as exc:
            # No exception text is written; transport failures may contain secret-bearing URLs.
            with db.transaction(serialized=True) as c:
                attempt_row=c.execute(select(personalization_attempts).where(
                    personalization_attempts.c.candidate_id==row['id'],personalization_attempts.c.evidence_fingerprint==locals().get('fp',''),
                    personalization_attempts.c.operation_id==operation_id).with_for_update()).mappings().first()
                if attempt_row and attempt_row['status']=='RESERVED':
                    c.execute(personalization_attempts.update().where(personalization_attempts.c.id==attempt_row['id']).values(
                        status='UNKNOWN_SPEND',error_code=type(exc).__name__,finished_at=now()))
            kind=_failure_kind(exc)
            outcome='KNOWN_RESPONSE_REPLAY_REJECTED' if replaying_saved_response else kind
            release(row,'INCOMPLETE',type(exc).__name__,retry_kind=('PERMANENT_EVIDENCE' if attempt_row else kind),outcome=outcome)
            result['attempted']+=1;result['incomplete']+=1;result['failure_categories'][kind]=result['failure_categories'].get(kind,0)+1;result['failure_reasons'][type(exc).__name__]=1
        if result['budget_stopped'] or time.monotonic()>=deadline:
            result['budget_stopped']=result['budget_stopped'] or time.monotonic()>=deadline
            break

    for row in claimed:
        with db.transaction() as c:
            c.execute(candidates.update().where(candidates.c.id==row['id'],candidates.c.personalization_lease_token==lease_token)
                .values(personalization_lease_token=None,personalization_lease_until=None))
    with db.transaction(serialized=True) as c:
        actual=Decimal('0');reserved=Decimal('0')
        for attempt in c.execute(select(personalization_attempts.c.status,personalization_attempts.c.actual_cost_usd,
                personalization_attempts.c.reserved_usd).where(personalization_attempts.c.run_id==run_id)).all():
            actual+=Decimal(str(attempt.actual_cost_usd or 0))
            if attempt.status in {'RESERVED','UNKNOWN_SPEND'}: reserved+=Decimal(str(attempt.reserved_usd or 0))
        result['elapsed_seconds']=round(time.monotonic()-run_started,2)
        result['unresolved_reserved_usd']=str(reserved);result['actual_cost_usd']=str(actual)
        result['worst_case_reserved_usd']=str(c.scalar(select(personalization_runs.c.reserved_usd).where(personalization_runs.c.id==run_id)) or 0)
        result['remaining_backlog']=c.scalar(select(func.count()).select_from(candidates).where(backlog)) or 0
        if result['status']=='COMPLETED' and result['budget_stopped']: result['status']='BUDGET_STOPPED'
        c.execute(personalization_runs.update().where(personalization_runs.c.id==run_id).values(
            finished_at=now(),status=result['status'],actual_cost_usd=str(actual),detail=_safe_json(result)))
    return result
