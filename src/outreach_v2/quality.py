"""Shared evidence quality gate for invitation personalization."""
from __future__ import annotations
import re

GENERIC_STRENGTHS = {
    'the bounded sample contains factual business and service information',
    'the page describes resume building features',
    'the site provides useful information',
}
GENERIC_RECOMMENDATIONS = (
    'add a descriptive page title', 'improve your website', 'add more content',
    'optimize for ai', 'improve ai visibility', 'add a concise factual section',
    'state the main offering in plain language',
)
GENERIC_DESCRIPTIONS = ('the bounded sample contains factual business and service information',
    'a factual description of the business and its services', 'business and service information')
CODE_MARKERS = re.compile(r'(?:\bfunction\s*\(|=>\s*\{|\bwindow\.[A-Za-z_$]|\bdocument\.[A-Za-z_$]|\bconst\s+[A-Za-z_$]|\bvar\s+[A-Za-z_$]|\bexport\s+default\b|\b(?:padding|margin|display|font-size)\s*:\s*[^.;]+;|\{\s*"(?:@context|@type|url|image|sameAs)"\s*:)', re.I)
SENTENCE = re.compile(r'[A-Za-z]{3,}')

def quality_reasons(p):
    """Return stable failure codes; never rewrite observed text."""
    reasons=[]
    if not isinstance(p,dict): return ['INSUFFICIENT_EVIDENCE']
    description=str(p.get('business_description') or '').strip()
    strength=str(p.get('company_strength') or '').strip()
    if CODE_MARKERS.search(description) or len(description)>1800 and description.count('{')+description.count(';')>8:
        reasons.append('CODE_CONTAMINATION')
    words=set(x.lower() for x in SENTENCE.findall(description))
    if len(words)<8 or len(description)<55 or any(x in description.casefold() for x in GENERIC_DESCRIPTIONS) or not p.get('evidence') and not p.get('source_urls'):
        reasons.append('INSUFFICIENT_EVIDENCE')
    if CODE_MARKERS.search(strength) or strength.casefold().strip('. ') in GENERIC_STRENGTHS or len(set(SENTENCE.findall(strength)))<7:
        reasons.append('GENERIC_STRENGTH')
    evidence=p.get('evidence') if isinstance(p.get('evidence'),dict) else {}
    evidence_text=' '.join([description,str(evidence.get('visible_text_excerpt',''))]).casefold()
    stop={'homepage','website','page','visitors','business','company','presents','identifies','identify','identified','offering','service','services','main','direct','clear','useful','provides','states','specific','site','description','descriptions','describes','described','giving','heading','title','the','its','with','from','that','this','and','for'}
    def stem(w):
        w=w.lower()
        if len(w)>6 and w.endswith('ing'): return w[:-3]
        if len(w)>5 and w.endswith('ies'): return w[:-3]+'y'
        if len(w)>4 and w.endswith('s') and not w.endswith('ss'): return w[:-1]
        return w
    strength_terms={stem(w) for w in SENTENCE.findall(strength) if len(w)>3 and w.lower() not in stop}
    evidence_terms={stem(w) for w in SENTENCE.findall(evidence_text) if len(w)>3 and w.lower() not in stop}
    if len(strength_terms & evidence_terms)<2 and 'GENERIC_STRENGTH' not in reasons:
        reasons.append('GENERIC_STRENGTH')
    recommendations=p.get('recommendations') or []
    valid=False
    if isinstance(recommendations,list):
        for item in recommendations:
            if not isinstance(item,dict): continue
            problem=str(item.get('problem') or '').strip()
            suggestion=str(item.get('suggestion') or '').strip()
            excerpt=str(item.get('evidence_excerpt') or item.get('evidence') or '').strip()
            urls=item.get('source_urls') or []
            generic=any(marker in suggestion.casefold() for marker in GENERIC_RECOMMENDATIONS)
            if generic: continue
            if len(problem)<28 or len(suggestion)<28 or len(excerpt)<25 or not urls:
                continue
            # The observation must cite a concrete page element or excerpt and
            # the proposed action must describe an implementable change.
            concrete=bool(re.search(r'\b(title|heading|faq|question|pricing|price|service|product|link|image|schema|structured data|contact|location|navigation|answer|booking|appointment)\b', problem, re.I))
            action=bool(re.search(r'\b(add|include|label|describe|identify|link|publish|state|organize|clarify|provide|mark|connect|expand|show)\b', suggestion, re.I))
            issue_terms={stem(w) for w in SENTENCE.findall(problem) if len(w)>3 and w.lower() not in stop}
            evidence_terms={stem(w) for w in SENTENCE.findall(excerpt) if len(w)>3 and w.lower() not in stop}
            suggestion_terms={stem(w) for w in SENTENCE.findall(suggestion) if len(w)>3 and w.lower() not in stop}
            evidence_supported=len(issue_terms & evidence_terms)>=2
            relevant_action=bool((issue_terms & suggestion_terms)-{'add','include','provide','state','show','page'})
            if concrete and action and evidence_supported and relevant_action: valid=True; break
    if not valid:
        if recommendations and any(isinstance(x,dict) and any(m in str(x.get('suggestion','')).casefold() for m in GENERIC_RECOMMENDATIONS) for x in recommendations):
            reasons.append('GENERIC_RECOMMENDATION')
        else: reasons.append('INSUFFICIENT_EVIDENCE')
    return list(dict.fromkeys(reasons))
