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
    # Version 2 separates attributable factual personalization from GEO
    # observations. A truthful profile can be useful even when the permitted
    # page sample supports no defensible recommendation.
    if p.get('analysis_version') == 'structured-evidence-v2':
        def normalized(value):
            return ' '.join(str(value or '').casefold().split())
        stop={'page','site','website','the','its','with','from','that','this','and','for','add','include','provide','state','show'}
        def stem(w):
            w=w.lower()
            if len(w)>6 and w.endswith('ing'): return w[:-3]
            if len(w)>5 and w.endswith('ies'): return w[:-3]+'y'
            if len(w)>4 and w.endswith('s') and not w.endswith('ss'): return w[:-1]
            return w
        description=normalized(p.get('business_description'))
        strength=normalized(p.get('company_strength'))
        if CODE_MARKERS.search(description) or CODE_MARKERS.search(strength):
            reasons.append('CODE_CONTAMINATION')
        if len(set(SENTENCE.findall(description))) < 8 or len(description) < 55:
            reasons.append('INSUFFICIENT_EVIDENCE')
        if any(x in description for x in GENERIC_DESCRIPTIONS):
            reasons.append('INSUFFICIENT_EVIDENCE')
        if len(set(SENTENCE.findall(strength))) < 7 or strength.strip('. ') in GENERIC_STRENGTHS:
            reasons.append('GENERIC_STRENGTH')
        profile=p.get('evidence_profile') if isinstance(p.get('evidence_profile'),dict) else {}
        pages=profile.get('pages') if isinstance(profile.get('pages'),list) else []
        facts=profile.get('facts') if isinstance(profile.get('facts'),list) else []
        fact_map={str(item.get('id')):item for item in facts if isinstance(item,dict) and item.get('id')}
        source_set=set(p.get('source_urls') or [])
        if not pages or not fact_map or not source_set:
            reasons.append('INSUFFICIENT_EVIDENCE')
        if any(not isinstance(page,dict) or page.get('url') not in source_set for page in pages):
            reasons.append('INSUFFICIENT_EVIDENCE')
        def source_normalize(value):
            return ' '.join(str(value or '').split())
        for fact in fact_map.values():
            urls=fact.get('source_urls') or []
            excerpt=source_normalize(fact.get('evidence_excerpt'))
            # Historical evidence profiles predate the separate capture field;
            # their immutable excerpt is the only preserved source snapshot.
            # New profiles carry the larger original page/section capture.
            source_content=source_normalize(fact.get('source_content') or fact.get('evidence_excerpt'))
            if (not excerpt or not source_content or excerpt not in source_content or not urls or
                fact.get('assertion_type') not in {'FIRST_PARTY_PAGE_CLAIM','STRUCTURED_FIRST_PARTY_DATA',
                    'ATTRIBUTED_COMPANY_DESCRIPTION','ATTRIBUTED_COMPANY_STRENGTH'} or
                any(url not in source_set for url in urls)):
                reasons.append('INSUFFICIENT_EVIDENCE')
        support=p.get('claim_support') if isinstance(p.get('claim_support'),dict) else {}
        stop={'page','site','website','the','its','with','from','that','this','and','for','name','states','says'}
        def terms(value):
            return {stem(w) for w in SENTENCE.findall(normalized(value)) if len(w)>3 and w.lower() not in stop}
        for field in ('business_description','company_strength'):
            ids=support.get(field) or []
            if not ids or any(item not in fact_map for item in ids):
                reasons.append('INSUFFICIENT_EVIDENCE')
                continue
            if not any(fact_map[item].get('attribution') in {'COMPANY_STATED','PUBLISHED_STRUCTURED_DATA'} for item in ids):
                reasons.append('INSUFFICIENT_EVIDENCE')

        assessment=p.get('assessment') if isinstance(p.get('assessment'),dict) else {}
        recommendations=p.get('recommendations')
        geo_status=assessment.get('geo_observation')
        if not isinstance(recommendations,list):
            reasons.append('INSUFFICIENT_EVIDENCE')
        elif not recommendations:
            if geo_status != 'NOT_ESTABLISHED':
                reasons.append('INSUFFICIENT_EVIDENCE')
        else:
            if geo_status != 'SUPPORTED':
                reasons.append('INSUFFICIENT_EVIDENCE')
            for item in recommendations:
                if not isinstance(item,dict):
                    reasons.append('INSUFFICIENT_EVIDENCE'); continue
                urls=item.get('source_urls') or []
                excerpt=normalized(item.get('evidence_excerpt') or item.get('evidence'))
                problem=normalized(item.get('problem'))
                suggestion=normalized(item.get('suggestion'))
                generic=any(marker in suggestion for marker in GENERIC_RECOMMENDATIONS)
                pterms={stem(w) for w in SENTENCE.findall(problem) if len(w)>3 and w.lower() not in stop}
                eterms={stem(w) for w in SENTENCE.findall(excerpt) if len(w)>3 and w.lower() not in stop}
                stokens={stem(w) for w in SENTENCE.findall(suggestion) if len(w)>3 and w.lower() not in stop}
                if (generic or len(problem)<28 or len(suggestion)<28 or len(excerpt)<25 or not urls or
                    any(url not in source_set for url in urls) or len(pterms & eterms)<2 or
                    not bool((pterms & stokens)-{'add','include','provide','state','show','page'}) or
                    not item.get('observed_at')):
                    reasons.append('GENERIC_RECOMMENDATION' if generic else 'INSUFFICIENT_EVIDENCE')
        return list(dict.fromkeys(reasons))

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
    if p.get('analysis_version') == 'structured-evidence-v1':
        profile = p.get('evidence_profile') if isinstance(p.get('evidence_profile'), dict) else {}
        pages = profile.get('pages') if isinstance(profile.get('pages'), list) else []
        facts = profile.get('facts') if isinstance(profile.get('facts'), list) else []
        fact_map = {str(x.get('id')): x for x in facts if isinstance(x, dict) and x.get('id')}
        source_set = set(p.get('source_urls') or [])
        provenance_ok = len(pages) >= 2 and len(fact_map) >= 3
        for page in pages:
            if not isinstance(page, dict) or page.get('url') not in source_set:
                provenance_ok = False
        for fact in fact_map.values():
            urls = fact.get('source_urls') or []
            if not fact.get('statement') or not fact.get('evidence_excerpt') or not urls:
                provenance_ok = False
            if any(url not in source_set for url in urls):
                provenance_ok = False
        support = p.get('claim_support') if isinstance(p.get('claim_support'), dict) else {}
        for field in ('business_description', 'company_strength'):
            ids = support.get(field) or []
            if not ids or any(item not in fact_map for item in ids):
                provenance_ok = False
                continue
            evidence_text = ' '.join(fact_map[item].get('evidence_excerpt', '') for item in ids).casefold()
            claim_text = str(p.get(field) or '').casefold()
            claim_terms = {stem(w) for w in SENTENCE.findall(claim_text) if len(w) > 3 and w.lower() not in stop}
            source_terms = {stem(w) for w in SENTENCE.findall(evidence_text) if len(w) > 3 and w.lower() not in stop}
            if len(claim_terms & source_terms) < 3:
                provenance_ok = False
        for item in recommendations if isinstance(recommendations, list) else []:
            if not isinstance(item, dict):
                provenance_ok = False
                continue
            if any(url not in source_set for url in (item.get('source_urls') or [])):
                provenance_ok = False
            excerpt_terms = {stem(w) for w in SENTENCE.findall(str(item.get('evidence_excerpt') or '')) if len(w) > 3 and w.lower() not in stop}
            issue_terms = {stem(w) for w in SENTENCE.findall(str(item.get('problem') or '')) if len(w) > 3 and w.lower() not in stop}
            if len(issue_terms & excerpt_terms) < 3:
                provenance_ok = False
        if not provenance_ok:
            reasons.append('INSUFFICIENT_EVIDENCE')
    return list(dict.fromkeys(reasons))


def quality_decisions(personalization, body=None, record=None, candidate_name=None, website=None):
    """Return independent evidence and recipient-copy decisions."""
    def normalize(value): return ' '.join(str(value or '').casefold().split())
    def stem_local(w):
        w=w.lower()
        if len(w)>6 and w.endswith('ing'): return w[:-3]
        if len(w)>5 and w.endswith('ies'): return w[:-3]+'y'
        if len(w)>4 and w.endswith('s') and not w.endswith('ss'): return w[:-1]
        return w
    ignored={'page','site','website','the','its','with','from','that','this','and','for','name','states','says'}
    def terms(value):
        return {stem_local(w) for w in SENTENCE.findall(normalize(value)) if len(w)>3 and w.lower() not in ignored}
    evidence_reasons=quality_reasons(personalization)
    evidence_valid=not evidence_reasons
    copy=personalization.get('recipient_copy') if isinstance(personalization,dict) else None
    copy_reasons=[]
    if not isinstance(copy,dict):
        copy_reasons.append('MISSING_RECIPIENT_COPY')
    else:
        required=('business_summary','distinctive_detail','research_relevance')
        if any(not isinstance(copy.get(key),str) or len(copy[key].strip())<35 for key in required):
            copy_reasons.append('INCOMPLETE_RECIPIENT_COPY')
        joined=' '.join(str(copy.get(key) or '') for key in required).casefold()
        for phrase in ('business description:', 'company strength:', 'sampled pages',
                       'geo observation not established', 'bounded sample'):
            if phrase in joined:
                copy_reasons.append('INTERNAL_REVIEW_LANGUAGE')
        profile=personalization.get('evidence_profile',{}) if isinstance(personalization,dict) else {}
        facts={str(f.get('id')):f for f in profile.get('facts',[]) if isinstance(f,dict) and f.get('id')}
        for key in ('business_summary','distinctive_detail','research_relevance'):
            ids=(copy.get('support') or {}).get(key) or []
            excerpt=' '.join(str(facts.get(i,{}).get('evidence_excerpt') or '') for i in ids)
            mapped=[facts.get(str(i)) for i in ids]
            # Citation validity is deterministic; semantic entailment remains a
            # human judgment and is exposed separately below. Lexical overlap
            # was incorrectly rejecting concise paraphrases while accepting
            # unrelated text that happened to share three words.
            if (not ids or any(item is None for item in mapped) or
                any(not item.get('evidence_excerpt') or
                    ' '.join(str(item.get('evidence_excerpt')).split()) not in
                    ' '.join(str(item.get('source_content') or item.get('evidence_excerpt')).split())
                    for item in mapped) or
                any(item.get('attribution') not in {'COMPANY_STATED','PUBLISHED_STRUCTURED_DATA'}
                    for item in mapped) or
                any(url not in set(personalization.get('source_urls') or [])
                    for item in mapped for url in (item.get('source_urls') or []))):
                copy_reasons.append('UNSUPPORTED_RECIPIENT_SUMMARY')
            if any(len(str(facts.get(i,{}).get('evidence_excerpt') or ''))>180 and
                   str(facts.get(i,{}).get('evidence_excerpt') or '') in str(copy.get(key) or '') for i in ids):
                copy_reasons.append('SOURCE_TEXT_COPIED_TO_RECIPIENT_COPY')
    authorized_fixture_label=bool(record and record.get('fixture') is True and record.get('id')
                                  and record.get('name')==candidate_name and record.get('website')==website)
    if body is not None:
        body_text=str(body).casefold()
        forbidden=('business description:', 'company strength:', 'sampled pages',
                   'geo observation not established')
        if not authorized_fixture_label: forbidden=(*forbidden,'owner-controlled staging test')
        for phrase in forbidden:
            if phrase in body_text: copy_reasons.append('INTERNAL_REVIEW_LANGUAGE')
        if copy and any(str(copy.get(key) or '') not in str(body) for key in ('business_summary','distinctive_detail','research_relevance')):
            copy_reasons.append('PERSONALIZATION_MISSING_FROM_EMAIL')
    assessment=personalization.get('assessment',{}) if isinstance(personalization,dict) else {}
    return {'EVIDENCE_VALID':{'passed':evidence_valid,'reasons':evidence_reasons,
                              'meaning':'Deterministic provenance and quality checks passed; this is not a semantic entailment proof.'},
            'CLAIM_GROUNDING':{'status':'AUTOMATED_FILTERING',
                               'meaning':'Only source-derived supported statements are included; unresolved model claims are excluded and retained in the audit.',
                               'excluded_uncertain_claims':assessment.get('excluded_model_claims',[])},
            'OUTREACH_REVIEW_READY':{'passed':evidence_valid and not copy_reasons,
                                     'reasons':list(dict.fromkeys(copy_reasons)),
                                     'preview_only':True}}
