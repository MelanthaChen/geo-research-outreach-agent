import importlib.util
from pathlib import Path
import sqlite3


SERVICE_PATH = Path(__file__).resolve().parents[1] / "scripts/phase7_service.py"
spec = importlib.util.spec_from_file_location("phase7_service", SERVICE_PATH)
phase7_service = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(phase7_service)


def test_launchd_job_uses_explicit_caps_and_concurrency_four():
    job = phase7_service.build_plist(
        python="/project/.venv/bin/python",
        request_cap=250_000,
        time_cap_seconds=172_800,
    )

    args = job["ProgramArguments"]
    assert args[:3] == ["/usr/bin/caffeinate", "-ims", "/project/.venv/bin/python"]
    assert args[args.index("--request-cap") + 1] == "250000"
    assert args[args.index("--time-cap-seconds") + 1] == "172800"
    assert args[args.index("--concurrency") + 1] == "4"
    assert args[args.index("--chunk-request-budget") + 1] == "500"
    assert job["KeepAlive"] == {"SuccessfulExit": False}
    assert job["RunAtLoad"] is True


def test_stop_state_json_is_atomic_and_readable(tmp_path):
    path = tmp_path / "state.json"
    phase7_service._atomic_json(path, {"status": "running", "pid": 42})

    assert phase7_service._read_json(path) == {"status": "running", "pid": 42}
    assert not path.with_suffix(".json.tmp").exists()


def test_status_reads_persisted_state_when_launch_agent_is_stopped(tmp_path, monkeypatch):
    database = tmp_path / "phase7.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE phase7_runs(run_id INTEGER, finished_at TEXT)")
        db.execute("INSERT INTO phase7_runs VALUES(4, '2026-10-17T00:15:46Z')")
    state_path = tmp_path / "state.json"
    phase7_service._atomic_json(state_path, {"status": "stopped", "stop_reason": "quality_degradation_pause"})
    monkeypatch.setattr(phase7_service, "STATE_PATH", state_path)
    monkeypatch.setattr(phase7_service, "_summary", lambda _path: {
        "pool_records": 12, "unique_canonical_domains": 12, "phase7_selected": 10,
        "phase7_processed": 8, "pending_unattempted": 2, "contactable_companies": 3,
        "contactable_with_email": 2, "contactable_with_confirmed_form": 2,
        "http_attempts": 21, "phase7_runtime_seconds": 50,
    })
    monkeypatch.setattr(phase7_service, "_launch_description", lambda: (False, None, "not_loaded"))
    monkeypatch.setattr(phase7_service, "_plist_settings", lambda: (100, 200, database))
    monkeypatch.setattr(phase7_service, "_last_site_error", lambda: None)
    monkeypatch.setattr(phase7_service, "integrity", lambda _path: "ok")

    result = phase7_service.status(database=database)

    assert result["service_running"] is False
    assert result["worker_state"] == "stopped"
    assert result["last_stop_reason"] == "quality_degradation_pause"
    assert result["processed_candidates"] == 8
    assert result["last_successful_checkpoint"]["run_id"] == 4
