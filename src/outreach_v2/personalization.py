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

from .db import candidates, contacts, events, invitations, now
from .migrate import dump
from .service import email_contacts
from .template import same_site, validate
from .quality import CODE_MARKERS

MAX_PAGE_BYTES = 512_000
# A fourth same-site page gives evidence-limited companies another grounded
# source opportunity while preserving the strict per-page size and time bounds.
MAX_PAGES_PER_CANDIDATE = 4
LEASE_SECONDS = 300


def public_url(url):
    p = urlparse(url)
    if p.scheme not in {'https', 'http'} or not p.hostname or p.username or p.password:
        raise ValueError('Invalid public website URL')
    addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(r[4][0]).is_global for r in addresses):
        raise ValueError('Website resolves to a non-public address')


def fetch_page(url):
    public_url(url)
    with httpx.Client(timeout=5, follow_redirects=False,
                       headers={'User-Agent': 'GEOResearchOutreach/2.0 (bounded invitation evidence)'}) as client:
        from urllib.robotparser import RobotFileParser
        parsed = urlparse(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        robots = client.get(origin + '/robots.txt')
        if robots.status_code == 200:
            rules = RobotFileParser()
            rules.parse(robots.text.splitlines())
            if not rules.can_fetch('GEOResearchOutreach', url):
                raise ValueError('Website robots rules disallow this page')
        elif robots.status_code not in {404, 410}:
            # Preserve the HTTP status so 429/5xx failures receive the same
            # bounded retry policy as other transient site responses.
            robots.raise_for_status()
            raise ValueError('Website robots policy could not be checked')
        target = url
        for _ in range(3):
            public_url(target)
            with client.stream('GET', target) as response:
                if response.is_redirect:
                    new = urljoin(target, response.headers.get('location', ''))
                    if not same_site(url, new):
                        raise ValueError('Redirect leaves the verified website')
                    target = new
                    continue
                response.raise_for_status()
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
    for tag in soup.find_all(True):
        style=(tag.get('style') or '').replace(' ','').lower()
        if tag.has_attr('hidden') or tag.get('aria-hidden')=='true' or 'display:none' in style or 'visibility:hidden' in style:
            tag.decompose()
    text=' '.join(soup.get_text(' ', strip=True).split())
    if structured:
        text=' '.join(dict.fromkeys([text,*structured]))
    return soup, text


def _evidence_urls(home_url, html):
    """Follow only explicit, relevant links from the verified homepage."""
    soup = BeautifulSoup(html, 'html.parser')
    priorities = ('about', 'service', 'product', 'solution', 'faq', 'question', 'pricing', 'menu')
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


def observe(url, html, stamp, name=None, truncated=False, additional_pages=()):
    pages = [(url, html, truncated), *additional_pages]
    parsed = [_page_text(item[1]) for item in pages]
    descriptions = []
    rec = []
    combined_text = []
    question_pattern = re.compile(r'\b(faq|frequently asked questions|hours|pricing|price list|how to|what to)\b', re.I)
    service_pattern = re.compile(r'\b(products?|services?|solutions?|menu|repairs?|classes|appointments?|rentals?|consulting|treatments?)\b', re.I)
    for (page_url, _, was_truncated), (soup, body_text) in zip(pages, parsed):
        description_tag = soup.find('meta', attrs={'name': 'description'})
        description = description_tag.get('content', '').strip() if description_tag else ''
        if CODE_MARKERS.search(description): description=''
        if not description:
            description = next((p.get_text(' ', strip=True) for p in soup.find_all('p')
                                if 70 <= len(p.get_text(' ', strip=True)) <= 700), '')
        if not description and len(body_text) >= 100:
            description = body_text[:600]
        if description:
            descriptions.append(description)
        combined_text.append(body_text)
        if not soup.find('title') or not soup.find('title').get_text(strip=True):
            path=urlparse(page_url).path or '/'
            rec.append({'title': 'Missing page title', 'problem': f'The rendered page at {path} has no non-empty HTML title element.',
                         'suggestion': f'Add an HTML title for {path} that names the business and this page topic.',
                         'evidence_excerpt': f'Observed HTML title element is absent at {page_url}. Visible content: {body_text[:250]}', 'source_urls': [page_url], 'observed_at': stamp})
        if not description_tag:
            path=urlparse(page_url).path or '/'
            rec.append({'title': 'Missing search description', 'problem': f'The rendered page at {path} has no meta description element.',
                         'suggestion': f'Add a meta description for {path} that accurately summarizes the visible business content.',
                         'evidence_excerpt': f'Observed meta description is absent at {page_url}. Visible content: {body_text[:250]}', 'source_urls': [page_url], 'observed_at': stamp})
    if not descriptions:
        raise ValueError('A grounded business description cannot yet be established from the sampled same-site pages')
    visible = ' '.join(combined_text)
    if not question_pattern.search(visible):
        rec.append({'title': 'Answer common customer questions',
                    'problem': 'No FAQ or direct answers about hours, pricing, or common customer questions appear in the sampled same-site pages.',
                    'suggestion': 'Add a concise factual section that answers the questions customers need before choosing the business.',
                    'evidence_excerpt': visible[:500], 'source_urls': [item[0] for item in pages], 'observed_at': stamp})
    if not service_pattern.search(visible):
        rec.append({'title': 'Describe the specific offering',
                    'problem': 'The sampled same-site pages do not explicitly name a product, service, or offering.',
                    'suggestion': 'State the main offering in plain language and connect it to the customer need it serves.',
                    'evidence_excerpt': visible[:500], 'source_urls': [item[0] for item in pages], 'observed_at': stamp})
    if not rec:
        raise ValueError('No V1 recommendation is supported by this bounded same-site sample; preserve existing data and defer')
    heading = parsed[0][0].find('h1')
    heading = heading.get_text(' ', strip=True) if heading else ''
    description = descriptions[0]
    return {'data_classification': 'OBSERVED', 'business_description': description,
            'company_strength': (f'The homepage identifies its main offering in the heading “{heading}”, giving visitors a direct description of what the business presents.' if heading
                                 else f'The homepage description states: “{description[:240]}”, which gives visitors a direct summary of the business offering.'),
            'source_urls': [item[0] for item in pages], 'retrieved_at': stamp,
            'recommendations': rec[:2],
            'evidence': {'description_quote': description, 'heading_quote': heading,
                         'visible_text_excerpt': visible[:1200], 'sampled_urls': [item[0] for item in pages],
                         'content_sample_truncated': any(item[2] for item in pages)}}


def _sample(fetcher, website, stamp):
    first = fetcher(website)
    home_url, home_html = first[:2]
    if not same_site(website, home_url):
        raise ValueError('Homepage redirect leaves the verified website')
    pages = [(home_url, home_html, first[2] if len(first) > 2 else False)]
    for target in _evidence_urls(home_url, home_html):
        try:
            fetched = fetcher(target)
            page_url, page_html = fetched[:2]
            if same_site(website, page_url):
                pages.append((page_url, page_html, fetched[2] if len(fetched) > 2 else False))
        except Exception as exc:
            # A failed secondary page does not erase usable homepage evidence.
            if isinstance(exc, httpx.TransportError) or isinstance(exc, httpx.HTTPStatusError):
                continue
            if isinstance(exc, ValueError) and ('robots' in str(exc).lower() or 'HTML' in str(exc)):
                continue
            raise
        if len(pages) >= MAX_PAGES_PER_CANDIDATE:
            break
    return observe(home_url, home_html, stamp, additional_pages=pages[1:])


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


def backfill(db, limit=25, fetcher=fetch_page, *, concurrency=4, time_budget_seconds=150):
    if not 1 <= limit <= 100:
        raise ValueError('Backfill request budget must be between 1 and 100 websites')
    if not 1 <= concurrency <= 8 or not 1 <= time_budget_seconds <= 300:
        raise ValueError('Backfill concurrency or runtime budget is outside its safe range')
    stamp = now()
    lease_token = uuid.uuid4().hex
    lease_until = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    first_party_email_count = select(func.count(func.distinct(contacts.c.email))).where(
        email_contacts()).correlate(candidates).scalar_subquery()
    no_prior_invitation = ~exists(select(invitations.c.id).where(invitations.c.candidate_id == candidates.c.id))
    eligible = and_(candidates.c.kind == 'company', candidates.c.fixture.is_(False),
        candidates.c.website_status == 'VERIFIED', candidates.c.qualification.in_(['ELIGIBLE', 'QUALIFIED']),
        candidates.c.suppressed.is_(False), candidates.c.personalization_status != 'READY',
        or_(candidates.c.personalization_retry_after.is_(None), candidates.c.personalization_retry_after <= stamp),
        or_(candidates.c.personalization_lease_until.is_(None), candidates.c.personalization_lease_until < stamp),
        first_party_email_count == 1, no_prior_invitation)
    backlog = and_(candidates.c.kind == 'company', candidates.c.fixture.is_(False),
        candidates.c.website_status == 'VERIFIED', candidates.c.qualification.in_(['ELIGIBLE', 'QUALIFIED']),
        candidates.c.suppressed.is_(False), candidates.c.personalization_status != 'READY',
        first_party_email_count == 1, no_prior_invitation)
    claimed = []
    domains = set()
    domain_cooldown = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    with db.transaction(serialized=True) as c:
        recently_processed = set(c.execute(select(candidates.c.domain).select_from(
            events.join(candidates, events.c.entity_id == candidates.c.id)).where(
                events.c.event.in_(['PERSONALIZATION_READY', 'PERSONALIZATION_INCOMPLETE']),
                events.c.created_at >= domain_cooldown)).scalars())
        recently_processed = {_domain_key(domain) for domain in recently_processed if domain}
        query = select(candidates).where(eligible).order_by(
            candidates.c.personalization_at.asc().nulls_first(), candidates.c.id).limit(min(1000, limit * 20))
        if db.postgres:
            query = query.with_for_update(skip_locked=True)
        for item in c.execute(query).mappings():
            row = dict(item)
            domain = _domain_key(row.get('domain'))
            if domain in domains or domain in recently_processed:
                continue
            result = c.execute(candidates.update().where(candidates.c.id == row['id'], eligible).values(
                personalization_lease_token=lease_token, personalization_lease_until=lease_until))
            if result.rowcount == 1:
                claimed.append(row)
                domains.add(domain)
            if len(claimed) == limit:
                break

    result = {'attempted': 0, 'completed': 0, 'incomplete': 0, 'permanent_insufficiency': 0,
              'transient_failures': 0, 'failure_categories': {}, 'failure_reasons': {}, 'elapsed_seconds': 0,
              'remaining_backlog': 0}
    if not claimed:
        with db.transaction() as c:
            result['remaining_backlog'] = c.scalar(select(func.count()).select_from(candidates).where(backlog)) or 0
        return result
    started = time.monotonic()
    deadline = started + time_budget_seconds

    def process_candidate(row):
        observed_at = now()
        try:
            personalization = _sample(fetcher, row['website'], observed_at)
            validate(personalization, row['website'])
            return row, {'personalization': dump(personalization), 'personalization_status': 'READY',
                         'personalization_quality_status': 'PASS','personalization_quality_errors':'[]',
                         'personalization_at': observed_at, 'personalization_error': None,
                         'personalization_outcome': 'READY', 'personalization_retry_after': None,
                         'description': personalization['business_description']}, None
        except Exception as exc:
            from .quality import quality_reasons
            kind = _failure_kind(exc)
            attempts = int(row.get('personalization_attempts') or 0) + 1
            if isinstance(exc, ValueError):
                reason = str(exc)[:300]
            elif isinstance(exc, httpx.HTTPStatusError):
                reason = f'HTTP {exc.response.status_code}'
            else:
                reason = type(exc).__name__
            quality_errors=quality_reasons(locals().get('personalization',{})) if 'personalization' in locals() else []
            return row, {'personalization_status': 'INCOMPLETE', 'personalization_at': observed_at,
                         'personalization_quality_status':'FAIL' if quality_errors else 'UNVERIFIED',
                         'personalization_quality_errors':dump(quality_errors),
                         'personalization_error': reason, 'personalization_outcome': kind,
                         'personalization_attempts': attempts,
                         'personalization_retry_after': _retry_time(kind, attempts, observed_at)}, kind

    outcomes = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)
    futures = {executor.submit(process_candidate, row): row for row in claimed}
    done, pending = concurrent.futures.wait(futures, timeout=max(0, deadline - time.monotonic()))
    for future in done:
        try:
            outcomes.append(future.result())
        except Exception as exc:  # Keep an unexpected worker exception categorized and recoverable.
            row = futures[future]
            kind = 'TRANSIENT_HTTP'
            at = now()
            attempts = int(row.get('personalization_attempts') or 0) + 1
            outcomes.append((row, {'personalization_status': 'INCOMPLETE', 'personalization_at': at,
                'personalization_error': type(exc).__name__, 'personalization_outcome': kind,
                'personalization_attempts': attempts, 'personalization_retry_after': _retry_time(kind, attempts, at)}, kind))
    for future in pending:
        future.cancel()
    executor.shutdown(wait=False, cancel_futures=True)

    with db.transaction(serialized=True) as c:
        for row, values, kind in outcomes:
            updated = c.execute(candidates.update().where(candidates.c.id == row['id'],
                candidates.c.personalization_lease_token == lease_token,
                candidates.c.personalization_status != 'READY').values(**values,
                personalization_lease_token=None, personalization_lease_until=None))
            if updated.rowcount != 1:
                continue
            result['attempted'] += 1
            if values['personalization_status'] == 'READY':
                result['completed'] += 1
            else:
                result['incomplete'] += 1
                result['failure_categories'][kind] = result['failure_categories'].get(kind, 0) + 1
                reason = values.get('personalization_error') or 'Unspecified failure'
                result['failure_reasons'][reason[:140]] = result['failure_reasons'].get(reason[:140], 0) + 1
                if kind == 'PERMANENT_EVIDENCE':
                    result['permanent_insufficiency'] += 1
                else:
                    result['transient_failures'] += 1
            c.execute(events.insert().values(id=uuid.uuid4().hex, entity_id=row['id'],
                event='PERSONALIZATION_' + values['personalization_status'],
                detail=dump({'classification': values.get('personalization_outcome'),
                    'error': values.get('personalization_error'), 'retry_after': values.get('personalization_retry_after'),
                    'website': row['website'], 'source_ref': row.get('source_ref'),
                    'sampled_urls': (values.get('personalization') and
                        __import__('json').loads(values['personalization']).get('source_urls'))}), created_at=values['personalization_at']))
        # Claims that never started are safe to release. In-flight work keeps its
        # lease until expiry so another scheduler cannot duplicate the fetch.
        for future in pending:
            if future.cancelled():
                row = futures[future]
                c.execute(candidates.update().where(candidates.c.id == row['id'],
                    candidates.c.personalization_lease_token == lease_token).values(
                    personalization_lease_token=None, personalization_lease_until=None))
    result['elapsed_seconds'] = round(time.monotonic() - started, 2)
    with db.transaction() as c:
        result['remaining_backlog'] = c.scalar(select(func.count()).select_from(candidates).where(backlog)) or 0
    return result
