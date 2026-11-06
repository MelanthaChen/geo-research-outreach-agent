import httpx
import pytest

from outreach_v2.integrations import Engine, OutcomeUncertain


def test_advance_and_reconcile_use_only_their_bounded_timeouts(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/outreach-jobs/key"):
            return httpx.Response(404, json={})
        if request.url.path.endswith("/outreach-capabilities"):
            return httpx.Response(200, json={"durable_jobs": True})
        return httpx.Response(200, json={"status": "QUEUED"})

    monkeypatch.setenv("GEO_ENGINE_API_URL", "https://engine.example")
    monkeypatch.setenv("GEO_ENGINE_HANDOFF_TOKEN", "test-token")
    transport = httpx.MockTransport(respond)
    # Keep the real 40-second client default while capturing every request.
    engine = Engine()
    engine.client.close()
    engine.client = httpx.Client(
        base_url="https://engine.example",
        headers={"Authorization": "Bearer test-token"},
        timeout=40,
        transport=transport,
    )

    engine.advance("key")
    engine.reconcile("key")
    engine.start("key", {"profile_id": "p"})

    advance = next(r for r in requests if r.url.path.endswith("/outreach-advance/key"))
    reconcile_requests = [r for r in requests if r.method == "GET"]
    start_post = next(r for r in requests if r.method == "POST" and r.url.path.endswith("/outreach-jobs"))
    assert advance.extensions["timeout"] == {
        "connect": 10.0,
        "read": 600.0,
        "write": 600.0,
        "pool": 600.0,
    }
    assert reconcile_requests
    assert all(r.extensions["timeout"] == {
        "connect": 10.0,
        "read": 120.0,
        "write": 120.0,
        "pool": 120.0,
    } for r in reconcile_requests)
    assert start_post.extensions["timeout"]["read"] == 40.0
    engine.client.close()


def test_advance_transport_timeout_is_uncertain_and_never_reposted():
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("Engine response timed out", request=request)

    engine = Engine.__new__(Engine)
    engine.client = httpx.Client(
        base_url="https://engine.example",
        timeout=40,
        transport=httpx.MockTransport(timeout),
    )
    with pytest.raises(OutcomeUncertain, match="reconcile the durable job") as raised:
        engine.advance("key")

    assert isinstance(raised.value.__cause__, httpx.ReadTimeout)
    assert len(calls) == 1
    engine.client.close()
