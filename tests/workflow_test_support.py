"""Stored, isolated evidence fixtures for invitation generation tests."""
import json
from outreach_agent.research_lifecycle import _connect

def add_personalization(path, profile_id):
    with _connect(path) as db:
        url=db.execute('SELECT official_website FROM research_records WHERE profile_id=?',(profile_id,)).fetchone()[0]
        payload={'data_classification':'OBSERVED','business_description':'Unit test description','company_strength':'Unit test strength','source_urls':[url],'retrieved_at':'2026-10-27T12:00:00Z','recommendations':[{'title':'Unit test observation','problem':'Unit test evidence gap','suggestion':'Unit test recommendation','source_urls':[url],'observed_at':'2026-10-27T12:00:00Z'}]}
        db.execute("UPDATE research_records SET personalization_json=?,identity_verification_status='VERIFIED' WHERE profile_id=?",(json.dumps(payload),profile_id))
