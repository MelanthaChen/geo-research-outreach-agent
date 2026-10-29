"""Frozen manual-invitation-v1/evidence-2, extracted without legacy state gates."""
from __future__ import annotations
import json, re
import os
from urllib.parse import urlparse
VERSION = 'manual-invitation-v1/evidence-2'
BULK_SUBJECT = 'GEO research participation invitation'  # Authorized shared Word subject.
REQUIRED = ('business_description','company_strength','source_urls','retrieved_at','recommendations')
EMAIL = re.compile(r'^[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$')
_TEMPLATE_VARIABLE = re.compile(r'\{\{([a-z][a-z0-9_]*)\}\}')

def render_template(template, values):
    """Substitute only template-owned variables; inserted company text stays literal."""
    variables=_TEMPLATE_VARIABLE.findall(template)
    missing=sorted(set(variables)-set(values))
    if missing:
        raise ValueError('Unresolved invitation variable: {{'+missing[0]+'}}')
    for name in variables:
        template=template.replace('{{'+name+'}}',str(values[name]))
    return template

def host(url):
    try:
        p=urlparse(url or '')
        return (p.hostname or '').lower().removeprefix('www.') if p.scheme in {'http','https'} else ''
    except ValueError: return ''

def same_site(a,b):
    return bool(host(a)) and host(a)==host(b)

def validate(personalization, website):
    if not isinstance(personalization,dict): raise ValueError('Personalization must be an object')
    if personalization.get('data_classification') != 'OBSERVED':
        raise ValueError('Observed personalization is missing')
    for field in REQUIRED:
        if field == 'recommendations' and personalization.get('analysis_version') == 'structured-evidence-v2':
            if not isinstance(personalization.get(field),list): raise ValueError('Invalid recommendations')
            if not personalization[field] and (personalization.get('assessment') or {}).get('geo_observation') != 'NOT_ESTABLISHED':
                raise ValueError('Empty observations require an explicit NOT_ESTABLISHED assessment')
            continue
        if not personalization.get(field): raise ValueError('Missing V1 field: '+field)
    urls=personalization['source_urls']
    if not isinstance(urls,list) or not all(same_site(website,u) for u in urls):
        raise ValueError('Personalization source belongs to another website')
    if not isinstance(personalization['recommendations'],list): raise ValueError('Invalid recommendations')
    for item in personalization['recommendations'][:2]:
        if not isinstance(item,dict) or not all(item.get(k) for k in ('problem','suggestion','source_urls','observed_at')):
            raise ValueError('Observation lacks problem, recommendation, evidence or timestamp')
        if not isinstance(item['source_urls'],list) or not all(same_site(website,u) for u in item['source_urls']):
            raise ValueError('Observation evidence belongs to another website')
    for finding in personalization.get('findings',[]):
        if 'percentage' in finding:
            n,d,p=finding.get('numerator'),finding.get('denominator'),finding['percentage']
            if not all(isinstance(x,(int,float)) and not isinstance(x,bool) for x in (n,d,p)) or d<=0 or not 0<=n<=d or abs(p-100*n/d)>.11 or not finding.get('source_urls'):
                raise ValueError('Numerical observation lacks a defensible sample')
            if not all(same_site(website,u) for u in finding['source_urls']): raise ValueError('Numerical evidence website mismatch')
    from .quality import quality_reasons
    failures=quality_reasons(personalization)
    if failures: raise ValueError('Personalization quality failed: '+','.join(failures))
    from .quality import quality_decisions
    decisions=quality_decisions(personalization)
    if not decisions['OUTREACH_REVIEW_READY']['passed']:
        raise ValueError('Outreach review quality failed: '+','.join(decisions['OUTREACH_REVIEW_READY']['reasons']))
    return personalization

def render(name, website, p, form_url, reference, record=None, identity=None):
    validate(p,website)
    identity=identity or {}
    researcher=(identity.get('researcher_name') or os.getenv('OUTREACH_RESEARCHER_NAME') or '').strip()
    affiliation=(identity.get('university_affiliation') or os.getenv('OUTREACH_UNIVERSITY_AFFILIATION') or '').strip()
    reply_to=(identity.get('reply_to') or os.getenv('OUTREACH_REPLY_TO') or '').strip()
    if not researcher or not affiliation or not EMAIL.fullmatch(reply_to):
        raise ValueError('Researcher name, university affiliation, and valid reply-to address must be configured')
    intro=str(p.get('introduction') or '')
    description=str(p.get('business_description') or '')
    is_fixture=bool(record and record.get('fixture') is True and record.get('id') and
                    record.get('name')==name and record.get('website')==website)
    label='OWNER-CONTROLLED STAGING TEST - NOT PRODUCTION OUTREACH\n\n' if is_fixture else ''
    copy=p.get('recipient_copy') or {}
    business_summary=copy.get('business_summary') or ''
    distinctive_detail=copy.get('distinctive_detail') or ''
    research_relevance=copy.get('research_relevance') or ''
    observation=(f'One detail that stood out: {copy.get("supported_observation")}\n\n'
                 if copy.get('supported_observation') else '')
    body_template=('{{label}}Hello,\n\n'
          'We are inviting {{company_name}} to participate in a university study about how AI-powered search represents businesses and their offerings.\n\n'
          '{{business_summary}} {{distinctive_detail}}\n\n'
          '{{research_relevance}}\n\n'
          '{{observation}}'
          'The study considers publicly available website information. It does not change or monitor your website, and we have not measured search performance or outcomes.\n\n'
          'Questionnaire: {{form_url}}\nInvitation reference: {{reference}}\n\n'
          'Please answer the research interest and research authorization questions separately. We will begin only after reviewing your responses and confirming that the study applies to your website.\n\n'
          'Best regards\n{{researcher_name}}\n{{university_affiliation}}\nReply to: {{reply_to}}')
    body=render_template(body_template,{
        'label':label,
        'company_name':str(name),'website':str(website),
        'business_summary':business_summary,
        'distinctive_detail':distinctive_detail,
        'research_relevance':research_relevance,
        'observation':observation,
        'form_url':str(form_url),'reference':str(reference),
        'researcher_name':researcher,'university_affiliation':affiliation,'reply_to':reply_to,
    })
    from .quality import quality_decisions
    decisions=quality_decisions(p,body,record=record,candidate_name=name,website=website)
    if not decisions['OUTREACH_REVIEW_READY']['passed']:
        raise ValueError('Rendered invitation is not review-ready: '+','.join(decisions['OUTREACH_REVIEW_READY']['reasons']))
    return f'GEO research participation invitation - {name}',body
