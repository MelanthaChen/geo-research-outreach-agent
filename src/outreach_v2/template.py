"""Frozen manual-invitation-v1/evidence-2, extracted without legacy state gates."""
from __future__ import annotations
import json, re
from urllib.parse import urlparse
VERSION = 'manual-invitation-v1/evidence-2'
BULK_SUBJECT = 'GEO research participation invitation'  # Authorized shared Word subject.
REQUIRED = ('business_description','company_strength','source_urls','retrieved_at','recommendations')
EMAIL = re.compile(r'^[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$')

def host(url):
    try:
        p=urlparse(url or '')
        return (p.hostname or '').lower().removeprefix('www.') if p.scheme in {'http','https'} else ''
    except ValueError: return ''

def same_site(a,b):
    return bool(host(a)) and host(a)==host(b)

def validate(personalization, website):
    if personalization.get('data_classification') != 'OBSERVED':
        raise ValueError('Observed personalization is missing')
    for field in REQUIRED:
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
    return personalization

def render(name, website, p, form_url, reference, fixture=False):
    validate(p,website)
    observations=[]
    for item in p['recommendations'][:2]:
        title=item.get('title') or item.get('observation') or 'Preliminary observation'
        urls=', '.join(item['source_urls'])
        observations.append(f"- {title}: {item['problem']} Recommendation: {item['suggestion']} ({urls}; observed {item['observed_at']})")
    intro=str(p.get('introduction') or '')
    description=str(p.get('business_description') or '')
    label='OWNER-CONTROLLED STAGING TEST - NOT PRODUCTION OUTREACH\n\n' if fixture else ''
    body=(f'{label}Hello,\n\n'
          f"{intro + chr(10) + chr(10) if intro else ''}"
          f'We are inviting {name} to participate in a research study about how public website information is represented in AI search. The website in this record is {website}.\n\n'
          f"{('Business description: ' + description + chr(10) + chr(10)) if description else ''}"
          f"Company strength: {p['company_strength']}\n\n"
          'Preliminary website observations already recorded in the research registry:\n'+'\n'.join(observations)+'\n\n'
          'These are observations and research hypotheses, not measured AI visibility results or guaranteed improvements. The proposed work is read-only public-website analysis; no website changes, experiments, or monitoring begin from this invitation.\n\n'
          f'Questionnaire: {form_url}\nInvitation reference: {reference}\n\n'
          'Please answer both research interest and research authorization separately. Detailed research starts only after valid consent and identity and scope checks.\n\n'
          'Best regards\nGEO Research Outreach')
    if re.search(r'\{\{?[^{}]+\}\}?',body): raise ValueError('Unresolved invitation variables')
    return f'GEO research participation invitation - {name}',body
