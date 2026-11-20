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
# Historical source URLs include descriptive contact routes such as
# ``/contact-and-directions/`` and ``/contact-information/``. Treat a
# contact-prefixed path as a contact route while leaving purpose restrictions
# to the existing bounded-context checks below.
CONTACT_PATH_PATTERN = r'(?:^|/)(?:contact(?:us|[-_][a-z0-9-]+)?|about|locations?)(?:/|$)'
CONTACT_PATH_RE = re.compile(CONTACT_PATH_PATTERN, re.I)
# Structured first-party Organization.email values are explicit public email
# declarations. Some sites publish these only as JSON-LD, without surrounding
# prose such as “general inquiries”; keep this bounded to the same object and
# require a contact/about route at the call site.
SCHEMA_ORG_TYPES = r'(?:organization|localbusiness|financialservice|bankorcreditunion|bankbranch|hotel|store)'
SCHEMA_ORG_EMAIL_PREFIX = r'"@type"\s*:\s*"' + SCHEMA_ORG_TYPES + r'"[^}]{0,250}"email"\s*:\s*"'
SCHEMA_ORG_EMAIL_REVERSE_PREFIX = r'"email"\s*:\s*"'
SCHEMA_ORG_EMAIL_REVERSE_SUFFIX = r'"[^}]{0,250}"@type"\s*:\s*"' + SCHEMA_ORG_TYPES + r'"'
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


def _schema_org_contact_email(email: str, evidence: str) -> bool:
    """Require an exact address in an Organization.email field in saved JSON-LD."""
    if not email or not isinstance(evidence, str):
        return False
    address = re.escape(email)
    pattern = re.compile(
        r'(?:' + SCHEMA_ORG_EMAIL_PREFIX + address + r'"|'
        + SCHEMA_ORG_EMAIL_REVERSE_PREFIX + address + SCHEMA_ORG_EMAIL_REVERSE_SUFFIX + r')', re.I)
    return bool(pattern.search(evidence))


def classify_public_contact_purpose(email: str, evidence: str = '', purpose: str = '', source_url: str = '') -> str:
    """Return normalized purpose only when preserved same-site context supports it.

    The email itself must be checked against first-party evidence by the caller.
    A generic address prefix never qualifies by itself. A contact/about route plus
    exact publication of a common business mailbox, exact Schema.org
    Organization.email data, or recognized nearby general-contact wording can
    qualify. Explicit role/context restrictions take precedence unless the same
    local text clearly identifies that exact address for general inquiries.
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

    # Schema.org's Organization.email is a positive publication signal on an
    # official contact route even when the adjacent Description is marketing
    # copy (or absent). It is deliberately not inferred from the address prefix.
    if CONTACT_PATH_RE.search(path) and _schema_org_contact_email(email, evidence):
        return GENERAL_LABEL

    # A common mailbox published on an official contact route is general
    # contact evidence even when a legacy excerpt contains only the address and
    # adjacent location details. Callers still require exact address matching
    # against the preserved first-party evidence; the local part alone never
    # qualifies. A route or nearby text that identifies a restricted purpose
    # was rejected above.
    parsed = urlparse(source_url or '')
    path = parsed.path or '/'
    generic_local = any(local == item or local.startswith(item+'.') or local.startswith(item+'_') or local.startswith(item+'-') for item in GENERAL_LOCALPARTS)
    exact_publication = email in (evidence or '').casefold() or email in (purpose or '').casefold()
    if CONTACT_PATH_RE.search(path) and generic_local and exact_publication and not restricted_context and not restricted_local:
        return GENERAL_LABEL
    return UNCLASSIFIED_LABEL
