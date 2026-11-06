import httpx
import pytest

from outreach_v2.integrations import Engine, OutcomeUncertain


def test_reconcile_timeout_is_bounded_and_does_not_change_other_requests(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET" and "/outreach-jobs/" in request.url.path:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"durable_jobs": True, "status": "QUEUED"})

    monkeypatch.setenv("GEO_ENGINE_API_URL", "https://engine.example")
    monkeypatch.setenv("GEO_ENGINE_HANDOFF_TOKEN", "test-token")
    engine = Engine()
    engine.client.close()
    engine.client = httpx.Client(
        base_url="https://engine.example",
        headers={"Authorization": "Bearer test-token"},
        timeout=40,
        transport=httpx.MockTransport(respond),
    )

    assert engine.reconcile("missing") is None
    engine.advance("key")
    engine.start("key", {"profile_id": "profile"})

    reconcile_requests = [r for r in requests if r.method == "GET"]
    assert len(reconcile_requests) == 4
    assert all(r.extensions["timeout"] == {
        "connect": 10.0,
        "read": 120.0,
        "write": 120.0,
        "pool": 120.0,
    } for r in reconcile_requests)
    advance = next(r for r in requests if r.url.path.endswith("/outreach-advance/key"))
    start = next(r for r in requests if r.method == "POST" and r.url.path.endswith("/outreach-jobs"))
    assert advance.extensions["timeout"]["read"] == 600.0
    assert start.extensions["timeout"]["read"] == 40.0
    engine.client.close()


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectTimeout])
def test_reconcile_transport_failure_is_recoverable_without_research_post(error_type):
    requests = []

    def fail(request):
        requests.append(request)
        raise error_type("Engine reconciliation timed out", request=request)

    engine = Engine.__new__(Engine)
    engine.client = httpx.Client(
        base_url="https://engine.example",
        timeout=40,
        transport=httpx.MockTransport(fail),
    )

    with pytest.raises(OutcomeUncertain, match="no research action initiated") as raised:
        engine.reconcile("key")

    assert isinstance(raised.value.__cause__, error_type)
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].extensions["timeout"]["read"] == 120.0
    engine.client.close()
