import json

import httpx
import pytest

from outreach_agent.wikidata import (
    SnapshotStore,
    build_sparql_query,
    diagnose_website_identity,
    is_third_party_profile,
    normalize_official_websites,
    parse_sparql_results,
    request_json_snapshot,
    wikidata_claim_values,
    write_csv_atomic,
)


def test_parser_groups_duplicate_qids_and_multiple_websites_deterministically():
    payload = {"results": {"bindings": [
        {"company": {"value": "http://www.wikidata.org/entity/Q20"}, "website": {"value": "https://two.example"}},
        {"company": {"value": "http://www.wikidata.org/entity/Q10"}, "website": {"value": "https://one.example"}},
        {"company": {"value": "http://www.wikidata.org/entity/Q20"}, "website": {"value": "https://two.example/about"}},
        {"company": {"value": "http://www.wikidata.org/entity/Q20"}, "website": {"value": "https://two.example"}},
    ]}}
    records = parse_sparql_results(payload)
    assert [row["qid"] for row in records] == ["Q10", "Q20"]
    assert records[1]["websites"] == ["https://two.example", "https://two.example/about"]


@pytest.mark.parametrize("payload", [{}, {"results": {"bindings": [{}]}}, {"results": {"bindings": "bad"}}])
def test_parser_rejects_missing_or_invalid_fields(payload):
    with pytest.raises(ValueError):
        parse_sparql_results(payload)


def test_domain_deduplication_normalizes_www_and_case():
    websites = normalize_official_websites(["https://www.Example.com/about", "http://example.com/", "mailto:x@example.com", "not a url"])
    assert websites == [("https://www.Example.com/about", "example.com")]


def test_claim_parser_handles_ids_quantities_dates_and_missing_values():
    entity = {"claims": {"P17": [{"mainsnak": {"datavalue": {"value": {"id": "Q30"}}}}],
                         "P1128": [{"mainsnak": {"datavalue": {"value": {"amount": "+125"}}}}],
                         "P571": [{"mainsnak": {"datavalue": {"value": {"time": "+2000-01-01T00:00:00Z"}}}}],
                         "P452": [{"mainsnak": {"snaktype": "somevalue"}}]}}
    assert wikidata_claim_values(entity, "P17") == ["Q30"]
    assert wikidata_claim_values(entity, "P1128") == ["125"]
    assert wikidata_claim_values(entity, "P571") == ["+2000-01-01T00:00:00Z"]
    assert wikidata_claim_values(entity, "P452") == []
    assert wikidata_claim_values(entity, "P131") == []


@pytest.mark.parametrize("url,expected", [("https://www.linkedin.com/company/acme", True), ("https://instagram.com/acme", True), ("https://acme.example", False)])
def test_third_party_profile_detection(url, expected):
    assert is_third_party_profile(url) is expected


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_success_is_persisted_atomically_before_replay_and_never_overwritten(tmp_path):
    store = SnapshotStore(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"ETag": "v1"})

    with _client(handler) as client:
        body, metadata, retries = request_json_snapshot(client, "https://example.test/query", params={"query": "q"}, store=store, batch_id="batch-1", sleep=lambda _: None)
        replay, replay_metadata, replay_retries = request_json_snapshot(client, "https://example.test/query", params={"query": "q"}, store=store, batch_id="batch-1", sleep=lambda _: None)
        store.save_success("batch-1", httpx.Response(200, content=b'{"changed":true}', request=httpx.Request("GET", "https://example.test")), params={}, request_timestamp="later")
    assert json.loads(body) == {"ok": True}
    assert body == replay
    assert metadata["response_headers"]["etag"] == "v1"
    assert replay_metadata == metadata
    assert retries == replay_retries == 0
    assert len(calls) == 1
    assert json.loads(store.paths("batch-1")[0].read_bytes()) == {"ok": True}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["batch-1.json", "batch-1.meta.json"]


@pytest.mark.parametrize("status", [429, 502, 503])
def test_transient_http_errors_retry_at_most_twice_and_honor_retry_after(tmp_path, status):
    store = SnapshotStore(tmp_path)
    calls, waits = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"rows": []})

    with _client(handler) as client:
        body, _, retries = request_json_snapshot(client, "https://example.test/query", params={}, store=store, batch_id=f"http-{status}", sleep=waits.append)
    assert json.loads(body) == {"rows": []}
    assert retries == 1
    assert len(calls) == 2
    assert waits == [1.0]


def test_retry_budget_caps_attempts_and_does_not_save_failed_response(tmp_path):
    store = SnapshotStore(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="unavailable")

    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        request_json_snapshot(client, "https://example.test/query", params={}, store=store, batch_id="failed", sleep=lambda _: None)
    assert len(calls) == 3
    assert not store.paths("failed")[0].exists()
    assert not store.paths("failed")[1].exists()


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_timeout_and_connection_failures_have_bounded_retries(tmp_path, error_type):
    store = SnapshotStore(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        raise error_type("transient", request=request)

    with _client(handler) as client, pytest.raises(error_type):
        request_json_snapshot(client, "https://example.test/query", params={}, store=store, batch_id="network-failure", sleep=lambda _: None)
    assert len(calls) == 3


def test_successful_snapshots_replay_offline_without_network(tmp_path):
    store = SnapshotStore(tmp_path)
    with _client(lambda request: httpx.Response(200, json={"value": 42})) as client:
        request_json_snapshot(client, "https://example.test/query", params={}, store=store, batch_id="saved", sleep=lambda _: None)
    with _client(lambda request: pytest.fail("offline replay made a network request")) as client:
        body, _, retries = request_json_snapshot(client, "https://example.test/query", params={}, store=store, batch_id="saved")
    assert json.loads(body) == {"value": 42}
    assert retries == 0


def test_queries_are_small_and_resume_by_stable_qid_not_offset():
    query = build_sparql_query("Q123", 20)
    assert "LIMIT 20" in query
    assert "Q123" in query
    assert "OFFSET" not in query.upper()
    with pytest.raises(ValueError):
        build_sparql_query(None, 50)


def test_csv_exports_are_byte_stable(tmp_path):
    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    rows = [{"qid": "Q1", "name": "One"}, {"qid": "Q2", "name": "Two"}]
    write_csv_atomic(first, ["qid", "name"], rows)
    write_csv_atomic(second, ["qid", "name"], rows)
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("args,expected", [
    (("Deep", "https://www.deep2001.com/", "VERIFIED"), "INSUFFICIENT_EVIDENCE"),
    (("Intel", "https://www.intel.com", "VERIFIED"), "VERIFIED"),
    (("Sogo Hong Kong", "http://www.sogo.com.hk/", "AMBIGUOUS"), "AMBIGUOUS_IDENTITY"),
    (("Julius Pintsch AG", "http://www.reuther-stc.com", "AMBIGUOUS"), "REJECTED_MISMATCH"),
    (("Q1001118", "https://fivosz.hu/", "AMBIGUOUS"), "INSUFFICIENT_EVIDENCE"),
    (("Intel", "https://www.intel.com", "BLOCKED"), "ACCESS_BLOCKED"),
    (("Intel", "https://www.intel.com", "FETCH_FAILED"), "FETCH_FAILED"),
])
def test_website_diagnostic_refines_saved_status_conservatively(args, expected):
    status = diagnose_website_identity(
        *args, http_status="403" if args[2] == "BLOCKED" else "200",
        final_url="https://menangsloto.com/" if args[0].startswith("Julius") else args[1],
        page_title="PRADA188 Judi Bola Togel Live Casino" if args[0].startswith("Julius") else ("DEEP & DEEP JEWELS" if args[0] == "Deep" else "Example title"),
    )
    assert status == expected


def test_ambiguous_identity_is_never_automatically_promoted():
    assert diagnose_website_identity(
        "Sogo Hong Kong", "https://www.sogo.com.hk/", "AMBIGUOUS",
        http_status="200", final_url="https://www.sogo.com.hk/tc", page_title="SOGO HK - Sogo",
    ) == "AMBIGUOUS_IDENTITY"
