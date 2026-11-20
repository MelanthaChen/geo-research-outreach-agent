"""Classify first-party email-purpose evidence without trusting local parts alone."""
from __future__ import annotations

import re
from urllib.parse import urlparse

GENERAL_LABEL = 'GENERAL_BUSINESS'
UNCLASSIFIED_LABEL = 'UNCLASSIFIED'
SPECIFIC_LABEL = 'PURPOSE_SPECIFIC'

# Kept in sync with the SQL expression in service.email_contact_evidence().
GENERAL_CONTEXT_PATTERN = r'(^|[^a-z0-9])(?:general\s+(business\s+)?(inquir(y|ies)|enquir(y|ies)|contact)|business\s+(inquir(y|ies)|enquir(y|ies)|contact)|all\s+(other\s+)?(questions|comments)|other\s+(questions|comments)|for\s+(all\s+)?(general\s+)?(questions|inquir(y|ies)|enquir(y|ies))|questions?\s+or\s+comments|get\s+in\s+touch|reach\s+(out|us)|contact\s+us|contact\s+page|email\s+us)([^a-z0-9]|$)'
RESTRICTED_CONTEXT_PATTERN = r'(^|[^a-z0-9])(?:press|media|public\s+relations|newsletter|subscribe|careers?|recruit(ing|ment)?|employment|jobs?|privacy|legal|reservations?|booking|concierge|events?|catering|wholesale|partnerships?|sales|education|training|orders?|support|helpdesk)([^a-z0-9]|$)'
GENERAL_CONTEXT_RE = re.compile(
    r'\b(?:general\s+(?:business\s+)?(?:inquir(?:y|ies)|enquir(?:y|ies)|contact)|'
    r'business\s+(?:inquir(?:y|ies)|enquir(?:y|ies)|contact)|'
    r'all\s+(?:other\s+)?(?:questions|comments)|other\s+(?:questions|comments)|'
    r'for\s+(?:all\s+)?(?:general\s+)?(?:questions|inquir(?:y|ies)|enquir(?:y|ies))|'
    r'questions?\s+or\s+comments|get\s+in\s+touch|reach\s+(?:out|us)|'
    r'contact\s+us|contact\s+page|email\s+us)\b', re.I)
RESTRICTED_CONTEXT_RE = re.compile(
    r'\b(?:press|media|public\s+relations|newsletter|subscribe|careers?|recruit(?:ing|ment)?|'
    r'employment|jobs?|privacy|legal|reservations?|booking|concierge|events?|catering|'
    r'wholesale|partnerships?|sales|education|training|orders?|support|helpdesk)\b', re.I)
RESTRICTED_LOCALPARTS = (
    'press','media','pr','newsletter','subscribe','career','careers','recruit','jobs','privacy','legal','abuse',
    'reservation','reservations','booking','concierge','events','catering','wholesale','partner','sales',
    'education','training','orders','support','helpdesk'
)
GENERAL_LOCALPARTS = ('info','contact','hello','enquiries','enquiry','office')
CONTACT_PATH_RE = re.compile(r'(?:^|/)(?:contact(?:-us)?|contactus|about|locations?)(?:/|$)', re.I)
PLACEHOLDER_HOSTS = ('example.com','example.org','example.net','test.com','test.org','mysite.com','email.com','invalid','localhost')


def _context_near_email(email: str, text: str, pattern: str, radius: int = 150) -> bool:
    """Match text within a bounded distance of the full address, not its local part."""
    if not email or not text:
        return False
    address = re.escape(email)
    return bool(re.search(
        rf'(?:{address}.{{0,{radius}}}{pattern}|{pattern}.{{0,{radius}}}{address})',
        text, re.I,
    ))


def classify_public_contact_purpose(email: str, evidence: str = '', purpose: str = '', source_url: str = '') -> str:
    """Return normalized purpose only when preserved same-site context supports it.

    The email itself must be checked against first-party evidence by the caller.
    A generic address prefix never qualifies by itself. A contact/about route plus
    a recognized general mailbox and contact-oriented surrounding text can qualify.
    Explicit role/context restrictions take precedence unless the same local text
    clearly identifies that exact address for general inquiries.
    """
    email = (email or '').strip().casefold()
    if not email or '@' not in email:
        return UNCLASSIFIED_LABEL
    local, host = email.rsplit('@', 1)
    host = host.rstrip('.')
    if host in PLACEHOLDER_HOSTS or host.endswith(('.example','.test','.invalid','.localhost')):
        return UNCLASSIFIED_LABEL

    # A normalized ingestion label is considered only after checking that the
    # locally preserved evidence does not indicate a restricted channel.
    if (purpose or '').strip().upper() in {SPECIFIC_LABEL, 'RESTRICTED', 'RESTRICTED_PURPOSE'}:
        return SPECIFIC_LABEL
    normalized_general = (purpose or '').strip().upper() == GENERAL_LABEL

    has_explicit_general = (
        _context_near_email(email, evidence, GENERAL_CONTEXT_PATTERN)
        or _context_near_email(email, purpose, GENERAL_CONTEXT_PATTERN)
    )
    path = urlparse(source_url or '').path

    restricted_local = any(local == item or local.startswith(item+'.') or local.startswith(item+'_') or local.startswith(item+'-') for item in RESTRICTED_LOCALPARTS)
    restricted_context = (
        _context_near_email(email, evidence, RESTRICTED_CONTEXT_PATTERN)
        or _context_near_email(email, purpose, RESTRICTED_CONTEXT_PATTERN)
        or bool(RESTRICTED_CONTEXT_RE.search(path))
    )
    if restricted_local or restricted_context:
        if not has_explicit_general:
            return SPECIFIC_LABEL

    if has_explicit_general or normalized_general:
        return GENERAL_LABEL

    # Contact-page context may make common published mailboxes suitable, but the
    # address prefix alone is insufficient. The saved excerpt must also describe
    # contacting/emailing the business (e.g., “Contact us” or “Email: ...”).
    parsed = urlparse(source_url or '')
    path = parsed.path or '/'
    contact_words = _context_near_email(
        email, evidence,
        r'(^|[^a-z0-9])(?:contact|email|questions?|inquiries|enquiries|office)([^a-z0-9]|$)',
    )
    generic_local = any(local == item or local.startswith(item+'.') or local.startswith(item+'_') or local.startswith(item+'-') for item in GENERAL_LOCALPARTS)
    if CONTACT_PATH_RE.search(path) and generic_local and contact_words and not restricted_context and not restricted_local:
        return GENERAL_LABEL
    return UNCLASSIFIED_LABEL
