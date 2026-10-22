import json
import sqlite3

from outreach_agent.hotel_website_discovery import discover_hotel_websites
from outreach_agent.hotels import init_hotel_db


class FakeResponse:
    url = "https://www.wikidata.org/w/api.php?action=wbgetentities"

    def __init__(self, body, status_code=200, headers=None):
        self._body = body
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.headers = headers or {}
        self.content = json.dumps(body, sort_keys=True).encode()

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, body, status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        return FakeResponse(self.body, self.status_code, self.headers)


def test_pilot_only_applies_exact_property_linked_p856_as_unverified(tmp_path, monkeypatch):
    database = tmp_path / "hotels.db"
    init_hotel_db(database)
    with sqlite3.connect(database) as db:
        db.execute("""INSERT INTO candidates(candidate_id,company_name,source_name,source_url,source_record_id,
          source_license_or_permission,discovery_timestamp,source_batch,verification_status)
          VALUES(?,?,?,?,?,?,?,?,?)""", ("hotel-1", "Molly Pitcher Inn", "OpenStreetMap US Northeast",
          "https://www.openstreetmap.org/node/1", "node/1", "ODbL", "2026-10-21", "fixture", "MISSING_OFFICIAL_WEBSITE"))
        db.commit()
    monkeypatch.setattr("outreach_agent.hotel_website_discovery._read_missing_candidates", lambda _: {
        "node/1": {"candidate_id": "hotel-1", "company_name": "Molly Pitcher Inn", "location": "Red Bank, NJ",
                   "source_record_id": "node/1", "source_url": "https://www.openstreetmap.org/node/1"}})
    monkeypatch.setattr("outreach_agent.hotel_website_discovery._osm_wikidata_links", lambda *_: {"node/1": "Q6896670"})
    output_dir = tmp_path / "discovery"
    monkeypatch.setattr("outreach_agent.hotel_website_discovery.DISCOVERY_ROOT", output_dir)
    monkeypatch.setattr("outreach_agent.hotel_website_discovery.PILOT_EXPORT", output_dir / "pilot.csv")
    monkeypatch.setattr("outreach_agent.hotel_website_discovery.SNAPSHOT_DIR", output_dir / "snapshots")
    body = {"entities": {"Q6896670": {"labels": {"en": {"value": "Molly Pitcher Inn"}}, "claims": {
        "P856": [{"mainsnak": {"datavalue": {"value": "https://www.mollypitcher.com/"}}}]}}}}
    client = FakeClient(body)

    result = discover_hotel_websites(pbf=tmp_path / "unused.pbf", database=database, limit=25, apply=True, client=client)

    assert result["selected"] == 1
    assert result["request_count"] == 1
    assert result["name_matched_urls"] == 1
    assert result["applied_to_hotel_db"] == 1
    assert len(client.calls) == 1
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT website,verification_status FROM candidates WHERE candidate_id='hotel-1'").fetchone()
        provenance = db.execute("SELECT source_name,source_record_id FROM hotel_provenance WHERE candidate_id='hotel-1'").fetchone()
    assert row == ("https://www.mollypitcher.com/", "DISCOVERED_UNVERIFIED")
    assert provenance == ("Wikidata official website claim (P856)", "Q6896670")
    assert result["snapshot"] and (tmp_path / "discovery" / "snapshots").exists()


def test_pilot_rejects_booking_aggregator_and_preserves_unresolved(tmp_path, monkeypatch):
    database = tmp_path / "hotels.db"
    init_hotel_db(database)
    monkeypatch.setattr("outreach_agent.hotel_website_discovery._read_missing_candidates", lambda _: {
        "node/1": {"candidate_id": "hotel-1", "company_name": "Example Hotel", "location": "X",
                   "source_record_id": "node/1", "source_url": "https://www.openstreetmap.org/node/1"}})
    monkeypatch.setattr("outreach_agent.hotel_website_discovery._osm_wikidata_links", lambda *_: {"node/1": "Q1"})
    output_dir = tmp_path / "discovery"
    monkeypatch.setattr("outreach_agent.hotel_website_discovery.DISCOVERY_ROOT", output_dir)
    monkeypatch.setattr("outreach_agent.hotel_website_discovery.PILOT_EXPORT", output_dir / "pilot.csv")
    monkeypatch.setattr("outreach_agent.hotel_website_discovery.SNAPSHOT_DIR", output_dir / "snapshots")
    body = {"entities": {"Q1": {"labels": {"en": {"value": "Example Hotel"}}, "claims": {
        "P856": [{"mainsnak": {"datavalue": {"value": "https://www.booking.com/hotel/example"}}}]}}}}

    result = discover_hotel_websites(pbf=tmp_path / "unused.pbf", database=database, client=FakeClient(body))

    assert result["discovered_urls"] == 0
    assert result["rows"][0]["discovery_status"] == "THIRD_PARTY_WEBSITE_REJECTED"
    assert result["rows"][0]["official_website"] == ""


def test_pilot_limit_is_capped_at_25(tmp_path):
    try:
        discover_hotel_websites(pbf=tmp_path / "unused.pbf", database=tmp_path / "unused.db", limit=26)
    except ValueError as exc:
        assert "between 1 and 25" in str(exc)
    else:
        raise AssertionError("pilot limit must be bounded")


def test_source_403_is_recorded_without_retry_or_database_update(tmp_path, monkeypatch):
    database = tmp_path / "hotels.db"
    init_hotel_db(database)
    monkeypatch.setattr("outreach_agent.hotel_website_discovery._read_missing_candidates", lambda _: {
        "node/1": {"candidate_id": "hotel-1", "company_name": "Example Hotel", "location": "X",
                   "source_record_id": "node/1", "source_url": "https://www.openstreetmap.org/node/1"}})
    monkeypatch.setattr("outreach_agent.hotel_website_discovery._osm_wikidata_links", lambda *_: {"node/1": "Q1"})
    output_dir = tmp_path / "discovery"
    monkeypatch.setattr("outreach_agent.hotel_website_discovery.DISCOVERY_ROOT", output_dir)
    monkeypatch.setattr("outreach_agent.hotel_website_discovery.PILOT_EXPORT", output_dir / "pilot.csv")
    monkeypatch.setattr("outreach_agent.hotel_website_discovery.SNAPSHOT_DIR", output_dir / "snapshots")
    client = FakeClient({}, 403)

    result = discover_hotel_websites(pbf=tmp_path / "unused.pbf", database=database, client=client)

    assert result["source_access_blocked"] is True
    assert result["http_status"] == 403
    assert result["request_count"] == len(client.calls) == 1
    assert result["discovered_urls"] == 0
    snapshot = json.loads(open(result["snapshot"], encoding="utf-8").read())
    assert snapshot["http_status"] == 403
    assert snapshot["outcome"] == "SOURCE_ACCESS_BLOCKED; no retry attempted"
