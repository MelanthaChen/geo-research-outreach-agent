from __future__ import annotations

import sqlite3
import json
import threading
from types import SimpleNamespace
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from outreach_agent.candidate_pool import (
    OSM_SOURCE, export_candidate_csv, import_osm_records, init_candidate_db,
    list_candidates, verify_candidate_batch,
)
from outreach_agent.dashboard import make_handler


def _databases(tmp_path: Path) -> tuple[Path, Path]:
    active, candidates = tmp_path / "active.db", tmp_path / "candidates.db"
    with sqlite3.connect(active) as db:
        db.execute("CREATE TABLE companies(normalized_domain TEXT)")
        db.executemany("INSERT INTO companies VALUES (?)", [("baseline.example.com",), ("www.existing.com",)])
    init_candidate_db(candidates)
    return active, candidates


def _rows() -> list[dict[str, str]]:
    base = {"company_name":"Example Business", "website":"https://example.biz", "canonical_domain":"example.biz", "industry":"shop=books", "location":"Boston, MA", "element_type":"node", "source_record_id":"node/1", "source_url":"https://www.openstreetmap.org/node/1"}
    return [base, {**base, "source_record_id":"way/2", "source_url":"https://www.openstreetmap.org/way/2"}, {**base, "company_name":"Existing", "canonical_domain":"existing.com", "source_record_id":"node/3"}]


def test_import_deduplicates_domains_and_preserves_provenance(tmp_path: Path) -> None:
    active, candidates = _databases(tmp_path)
    result = import_osm_records(_rows(), active, candidates, "a" * 64)
    assert result["parsed_records"] == 3
    assert result["active_duplicates"] == 1
    assert result["duplicate_domains"] == 1
    assert result["imported_unique"] == 1
    with sqlite3.connect(candidates) as db:
        row = db.execute("SELECT verification_status,geo_eligibility_status,contact_status,human_review_status,outreach_ready_status FROM candidates").fetchone()
        assert row == ("DISCOVERED_UNVERIFIED", "UNKNOWN", "NOT_DISCOVERED", "PENDING", "NOT_READY")
        assert db.execute("SELECT COUNT(*) FROM candidate_provenance").fetchone()[0] == 2
        assert db.execute("SELECT source_name FROM candidates").fetchone()[0] == OSM_SOURCE


def test_repeated_import_is_idempotent_and_csv_stable(tmp_path: Path) -> None:
    active, candidates = _databases(tmp_path)
    first_import = import_osm_records(_rows(), active, candidates, "b" * 64)
    assert first_import["imported_unique"] == 1
    first = export_candidate_csv("all_candidates", candidates, include_active=False)
    repeated = import_osm_records(_rows(), active, candidates, "b" * 64)
    assert repeated["imported_unique"] == 0
    assert export_candidate_csv("all_candidates", candidates, include_active=False) == first
    assert len(first.splitlines()) == 2
    assert "ODbL 1.0" in first


def test_candidate_search_source_filter_and_pagination_are_deterministic(tmp_path: Path) -> None:
    active, candidates = _databases(tmp_path)
    rows = _rows()
    rows.append({**rows[0], "company_name":"Another Business", "website":"https://another.biz",
                 "canonical_domain":"another.biz", "source_record_id":"node/4", "source_url":"https://www.openstreetmap.org/node/4"})
    import_osm_records(rows, active, candidates)
    first = list_candidates(candidates, page=1, page_size=1)
    second = list_candidates(candidates, page=1, page_size=1)
    searched = list_candidates(candidates, q="EXAMPLE", page=1, page_size=10)
    filtered = list_candidates(candidates, source=OSM_SOURCE, status="DISCOVERED_UNVERIFIED")
    assert first == second
    assert first["total"] == 2 and first["page_size"] == 1
    assert len(searched["candidates"]) == 1
    assert filtered["total"] == 2


def test_exports_do_not_promote_unverified_or_missing_contacts(tmp_path: Path) -> None:
    active, candidates = _databases(tmp_path)
    import_osm_records(_rows(), active, candidates)
    assert len(export_candidate_csv("verified_companies", candidates, include_active=False).splitlines()) == 1
    assert len(export_candidate_csv("geo_eligible_companies", candidates, include_active=False).splitlines()) == 1
    assert len(export_candidate_csv("contact_candidates", candidates, include_active=False).splitlines()) == 1


def test_bounded_verification_reuses_identity_and_qualification_without_contacts(tmp_path: Path, monkeypatch) -> None:
    active, candidates = _databases(tmp_path)
    import_osm_records(_rows(), active, candidates)

    class FakeCollector:
        website_requests_attempted = 2
        retry_attempts = 0
        def __init__(self, settings):
            self.settings = settings
        def inspect(self, company):
            page = SimpleNamespace(requested_url=company.website, final_url=company.website, http_status=200,
                                   title="Example Business", meta_description="Official Example Business",
                                   content_hash="abc", visible_text_length=1800, structured_data_types="[]", fetch_status="SUCCESS",
                                   visible_text="Example Business services")
            return SimpleNamespace(extracted_signals=json.dumps({"reachable":True,"meaningful_text":True,
                      "substantial_public_information":True,"online_discovery_relevance":True,
                      "pages_inspected":1}), fetch_status="SUCCESS", pages=[page], http_status=200,
                      error_type=None, final_url=company.website)
        def close(self):
            pass

    monkeypatch.setattr("outreach_agent.website.WebsiteCollector", FakeCollector)
    result = verify_candidate_batch(1, candidate_db=candidates)
    assert result["attempted"] == 1
    with sqlite3.connect(candidates) as db:
        row = db.execute("SELECT verification_status,geo_eligibility_status,contact_status,human_review_status,outreach_ready_status FROM candidates").fetchone()
    assert row == ("VERIFIED", "ELIGIBLE", "NOT_DISCOVERED", "PENDING", "NOT_READY")


def test_candidate_dashboard_endpoint_is_paginated_and_read_only(monkeypatch) -> None:
    observed = {}
    def fake_list(**kwargs):
        observed.update(kwargs)
        return {"candidates":[{"candidate_id":"osm:test","company_name":"Candidate","canonical_domain":"candidate.example"}],"total":1,"page":1,"page_size":10}
    monkeypatch.setattr("outreach_agent.dashboard.list_phase6e_candidates", fake_list)
    monkeypatch.setattr("outreach_agent.dashboard.phase6e_summary", lambda *args: {"candidate_count":1,"verified":0,"geo_eligible":0,"with_email":0,"outreach_ready":0,"screen_statuses":{}})
    monkeypatch.setattr("outreach_agent.dashboard.PHASE6E_DB", Path("/missing/phase6e.db"))
    monkeypatch.setattr("outreach_agent.dashboard.PHASE6H_DB", Path("/missing/phase6h.db"))
    monkeypatch.setattr("outreach_agent.dashboard.phase6h_summary", lambda: {})
    server = ThreadingHTTPServer(("127.0.0.1",0), make_handler({}))
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/candidates?q=Candidate&page=1&page_size=10") as response:
            payload=json.loads(response.read())
        assert payload["total"]==1 and payload["candidates"][0]["candidate_id"]=="osm:test"
        assert observed["q"]=="Candidate" and observed["page_size"]==10
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_unverified_identity_does_not_receive_automatic_geo_qualification(tmp_path: Path, monkeypatch) -> None:
    active, candidates = _databases(tmp_path)
    import_osm_records(_rows(), active, candidates)

    class FakeCollector:
        website_requests_attempted = 1
        retry_attempts = 0
        def __init__(self, settings): pass
        def inspect(self, company):
            page = SimpleNamespace(requested_url=company.website, final_url=company.website, http_status=200,
                title="Different Organization", meta_description="", content_hash="x", visible_text_length=2000,
                structured_data_types="[]", fetch_status="SUCCESS")
            return SimpleNamespace(extracted_signals='{"meaningful_text":true}', fetch_status="SUCCESS",
                pages=[page], http_status=200)
        def close(self): pass

    monkeypatch.setattr("outreach_agent.website.WebsiteCollector", FakeCollector)
    monkeypatch.setattr("outreach_agent.candidate_pool.verify_identity_for_candidate",
                        lambda company,snapshot: SimpleNamespace(status="AMBIGUOUS_IDENTITY",reason="Identity mismatch not established",identity_tokens=[]))
    result=verify_candidate_batch(1,candidate_db=candidates)
    assert result["attempted"]==1
    with sqlite3.connect(candidates) as db:
        status,eligibility,opportunity,evidence=db.execute("SELECT verification_status,geo_eligibility_status,geo_opportunity_status,qualification_evidence FROM candidates").fetchone()
    assert status=="AMBIGUOUS_IDENTITY"
    assert (eligibility,opportunity)==("NEEDS_REVIEW","UNKNOWN")
    assert json.loads(evidence)["status"]=="NOT_APPLIED_IDENTITY_UNVERIFIED"
