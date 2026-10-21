"""Install and manage the user-scoped launchd worker for Phase 7."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from outreach_agent.phase7 import PHASE7_DB, _summary, integrity, run

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.geo-research-outreach-agent.phase7"
AGENT_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = ROOT / "logs"
STDOUT_LOG = LOG_DIR / "phase7_worker.log"
STDERR_LOG = LOG_DIR / "phase7_worker.err.log"
STATE_PATH = ROOT / "data/phase7_service_state.json"
STOP_PATH = ROOT / "data/.phase7_stop.request"
DEFAULT_REQUEST_CAP = 250_000
DEFAULT_TIME_CAP_SECONDS = 172_800
RESUME_REQUEST_INCREMENT = 100_000
RESUME_TIME_INCREMENT_SECONDS = 86_400
MAX_RESTARTS_WITHOUT_PROGRESS = 3
CHUNK_REQUEST_BUDGET = 500
BATCH_SIZE = 500
CONCURRENCY = 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _last_site_error() -> str | None:
    try:
        with STDERR_LOG.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 8192))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return next((line[-1000:] for line in reversed(lines) if line.startswith("stage=website ") and " error=" in line), None)


def _run_count(db_path: Path = PHASE7_DB) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        return db.execute("SELECT COUNT(*) FROM phase7_runs").fetchone()[0]


def build_plist(*, python: str, request_cap: int = DEFAULT_REQUEST_CAP,
                time_cap_seconds: int = DEFAULT_TIME_CAP_SECONDS,
                database: Path = PHASE7_DB) -> dict[str, Any]:
    """Return a deterministic per-user launchd job definition."""
    return {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/caffeinate", "-ims", python,
            str(ROOT / "scripts/phase7_service.py"), "worker",
            "--database", str(database),
            "--request-cap", str(request_cap),
            "--time-cap-seconds", str(time_cap_seconds),
            "--batch-size", str(BATCH_SIZE),
            "--chunk-request-budget", str(CHUNK_REQUEST_BUDGET),
            "--concurrency", str(CONCURRENCY),
        ],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"PYTHONPATH": str(ROOT / "src")},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "ProcessType": "Background",
        "StandardOutPath": str(STDOUT_LOG),
        "StandardErrorPath": str(STDERR_LOG),
    }


def _launch_domain() -> str:
    return f"gui/{os.getuid()}"


def _service_target() -> str:
    return f"{_launch_domain()}/{LABEL}"


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["/bin/launchctl", *args], text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"launchctl {args[0]} failed")
    return result


def _launch_description() -> tuple[bool, int | None, str]:
    result = _launchctl("print", _service_target())
    if result.returncode:
        return False, None, "not_loaded"
    description = result.stdout
    pid = None
    for line in description.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            try:
                pid = int(stripped.split("=", 1)[1].strip())
            except ValueError:
                pass
            break
    return True, pid, description


def _write_plist(*, request_cap: int = DEFAULT_REQUEST_CAP,
                 time_cap_seconds: int = DEFAULT_TIME_CAP_SECONDS,
                 python: str | None = None, database: Path = PHASE7_DB) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("The Phase 7 background service requires macOS launchd.")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    job = build_plist(python=python or sys.executable, request_cap=request_cap,
                      time_cap_seconds=time_cap_seconds, database=database)
    with AGENT_PATH.open("wb") as stream:
        plistlib.dump(job, stream, sort_keys=True)
    lint = subprocess.run(["/usr/bin/plutil", "-lint", str(AGENT_PATH)], text=True, capture_output=True)
    if lint.returncode:
        raise RuntimeError(lint.stderr.strip() or lint.stdout.strip() or "launchd plist validation failed")


def _plist_settings() -> tuple[int, int, Path]:
    try:
        job = plistlib.loads(AGENT_PATH.read_bytes())
    except FileNotFoundError:
        return DEFAULT_REQUEST_CAP, DEFAULT_TIME_CAP_SECONDS, PHASE7_DB
    args = job.get("ProgramArguments", [])
    # Consume the serialized command-line options without depending on their order.
    values: dict[str, str] = {}
    for index, item in enumerate(args[:-1]):
        if item.startswith("--"):
            values[item] = args[index + 1]
    return (int(values.get("--request-cap", DEFAULT_REQUEST_CAP)),
            int(values.get("--time-cap-seconds", DEFAULT_TIME_CAP_SECONDS)),
            Path(values.get("--database", str(PHASE7_DB))))


def _bootstrap() -> None:
    _launchctl("enable", _service_target())
    _launchctl("bootstrap", _launch_domain(), str(AGENT_PATH), check=True)


def install() -> dict[str, Any]:
    if sys.platform != "darwin":
        raise RuntimeError("The Phase 7 background service requires macOS launchd.")
    loaded, pid, _ = _launch_description()
    if loaded and pid:
        raise RuntimeError(f"Phase 7 service is already running (PID {pid}); stop it before reinstalling.")
    _write_plist()
    STOP_PATH.unlink(missing_ok=True)
    if loaded:
        _launchctl("bootout", _service_target())
    _bootstrap()
    return {"service": LABEL, "status": "installed_and_started", "plist": str(AGENT_PATH)}


def start() -> dict[str, Any]:
    if not AGENT_PATH.exists():
        return install()
    loaded, pid, _ = _launch_description()
    if pid:
        return {"service": LABEL, "status": "already_running", "pid": pid}
    STOP_PATH.unlink(missing_ok=True)
    if loaded:
        _launchctl("kickstart", "-k", _service_target(), check=True)
    else:
        _bootstrap()
    return {"service": LABEL, "status": "start_requested"}


def stop(*, timeout_seconds: int = 300) -> dict[str, Any]:
    loaded, pid, _ = _launch_description()
    if not loaded:
        return {"service": LABEL, "status": "stopped", "message": "Service is not loaded."}
    STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    STOP_PATH.write_text("Stop after the active checkpoint.\n", encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        loaded, pid, _ = _launch_description()
        if not loaded or pid is None:
            _launchctl("bootout", _service_target())
            return {"service": LABEL, "status": "stopped", "graceful": True}
        time.sleep(2)
    return {"service": LABEL, "status": "stop_pending", "pid": pid,
            "message": "Worker did not reach a checkpoint before timeout; it remains loaded to avoid interruption."}


def resume() -> dict[str, Any]:
    loaded, pid, _ = _launch_description()
    if pid:
        return {"service": LABEL, "status": "already_running", "pid": pid,
                "message": "Worker is active; no restart was performed."}
    old_request_cap, old_time_cap, database = _plist_settings()
    new_request_cap = old_request_cap + RESUME_REQUEST_INCREMENT
    new_time_cap = old_time_cap + RESUME_TIME_INCREMENT_SECONDS
    if loaded:
        _launchctl("bootout", _service_target())
    _write_plist(request_cap=new_request_cap, time_cap_seconds=new_time_cap, database=database)
    STOP_PATH.unlink(missing_ok=True)
    _bootstrap()
    return {"service": LABEL, "status": "resumed", "request_cap": new_request_cap,
            "time_cap_seconds": new_time_cap}


def status(*, database: Path = PHASE7_DB) -> dict[str, Any]:
    summary = _summary(database)
    loaded, service_pid, description = _launch_description() if sys.platform == "darwin" else (False, None, "unsupported")
    worker_pid = service_pid
    state = _read_json(STATE_PATH)
    if service_pid is not None:
        processes = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,command="], text=True, capture_output=True)
        process_lines = [line.strip().split(None, 2) for line in processes.stdout.splitlines()]
        service_command = next((parts[2] for parts in process_lines
            if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) == service_pid), "")
        if "/usr/bin/caffeinate" in service_command:
            worker_pid = next((int(parts[0]) for parts in process_lines
                if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit()
                and int(parts[1]) == service_pid and "phase7_service.py worker" in parts[2]
                and "/usr/bin/caffeinate" not in parts[2]), service_pid)
    now = time.time()
    started = state.get("started_epoch")
    elapsed = max(0.0, now - float(started)) if service_pid and started else 0.0
    start_processed = int(state.get("start_processed", summary["phase7_processed"]))
    rate = ((summary["phase7_processed"] - start_processed) / elapsed * 3600) if elapsed > 0 else 0.0
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        checkpoint = db.execute("SELECT run_id,finished_at FROM phase7_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    request_cap, time_cap, _ = _plist_settings()
    return {
        "service": LABEL,
        "service_running": service_pid is not None,
        "service_loaded": loaded,
        "service_pid": service_pid,
        "worker_pid": worker_pid,
        "worker_state": state.get("status", "not_started"),
        "last_error": state.get("last_error"),
        "last_site_error": _last_site_error(),
        "last_stop_reason": None if service_pid is not None else state.get("stop_reason"),
        "total_candidate_rows": summary["pool_records"],
        "unique_candidate_domains": summary["unique_canonical_domains"],
        "selected_candidate_pool": summary["phase7_selected"],
        "processed_candidates": summary["phase7_processed"],
        "remaining_candidates": summary["pending_unattempted"],
        "strict_contactable_companies": summary["contactable_companies"],
        "business_email_companies": summary["contactable_with_email"],
        "contact_form_companies": summary["contactable_with_confirmed_form"],
        "http_attempts": summary["http_attempts"],
        "processing_candidates_per_hour_this_service": round(rate, 2),
        "cumulative_runtime_seconds": summary["phase7_runtime_seconds"],
        "last_successful_checkpoint": ({"run_id": checkpoint[0], "finished_at": checkpoint[1]}
                                       if checkpoint else None),
        "database_integrity": integrity(database),
        "request_cap": request_cap,
        "time_cap_seconds": time_cap,
        "log_file": str(STDOUT_LOG),
        "error_log_file": str(STDERR_LOG),
        "state_file": str(STATE_PATH),
        "launchd_state_excerpt": description[:1000] if loaded else None,
    }


def _worker(*, database: Path, request_cap: int, time_cap_seconds: int,
            batch_size: int, chunk_request_budget: int, concurrency: int) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    prior = _read_json(STATE_PATH)
    current_run_count = _run_count(database)
    start_run_count = current_run_count
    crash_count = int(prior.get("restart_failures_without_progress", 0))
    if prior.get("status") in {"running", "worker_failed"}:
        if current_run_count > int(prior.get("last_run_count", 0)):
            crash_count = 0
        else:
            crash_count += 1
    if crash_count >= MAX_RESTARTS_WITHOUT_PROGRESS:
        _atomic_json(STATE_PATH, {**prior, "status": "restart_limit_reached",
            "last_error": f"Stopped after {crash_count} launchd restarts without a saved processing chunk.",
            "updated_at": _now(), "restart_failures_without_progress": crash_count})
        return 0
    try:
        snapshot = _summary(database)
        _atomic_json(STATE_PATH, {**prior, "status": "running", "pid": os.getpid(),
            "started_at": _now(), "started_epoch": time.time(),
            "start_processed": snapshot["phase7_processed"], "last_run_count": current_run_count,
            "restart_failures_without_progress": crash_count, "last_error": None,
            "updated_at": _now(), "request_cap": request_cap, "time_cap_seconds": time_cap_seconds})
        for attempt in range(1, 4):
            try:
                result = run(db_path=database, batch_size=batch_size, request_cap=request_cap,
                    time_cap_seconds=time_cap_seconds, chunk_request_budget=chunk_request_budget,
                    concurrency=concurrency, stop_file=STOP_PATH, export_each_batch=True)
                _atomic_json(STATE_PATH, {**_read_json(STATE_PATH), "status": "stopped" if result["stop_reason"] != "selection_complete" else "complete",
                    "pid": None, "stop_reason": result["stop_reason"], "last_error": None,
                    "last_run_count": _run_count(database), "finished_at": _now(), "updated_at": _now(),
                    "final_processed": result["phase7_processed"],
                    "final_contactable": result["contactable_companies"],
                    "restart_failures_without_progress": 0})
                return 0
            except Exception as exc:  # bounded retry; run lock is released by its context manager
                _atomic_json(STATE_PATH, {**_read_json(STATE_PATH), "status": "running",
                    "last_error": f"Attempt {attempt}/3: {type(exc).__name__}: {exc}",
                    "last_run_count": start_run_count, "updated_at": _now()})
                traceback.print_exc()
                if attempt < 3:
                    time.sleep(15 * (2 ** (attempt - 1)))
        _atomic_json(STATE_PATH, {**_read_json(STATE_PATH), "status": "worker_failed", "pid": None,
            "last_run_count": start_run_count,
            "restart_failures_without_progress": crash_count, "updated_at": _now()})
        return 1
    except Exception as exc:
        _atomic_json(STATE_PATH, {**prior, "status": "worker_failed", "pid": None,
            "last_error": f"{type(exc).__name__}: {exc}", "updated_at": _now(),
            "last_run_count": start_run_count,
            "restart_failures_without_progress": crash_count})
        traceback.print_exc()
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "start", "stop", "status", "resume", "uninstall", "worker"))
    parser.add_argument("--database", type=Path, default=PHASE7_DB)
    parser.add_argument("--request-cap", type=int, default=DEFAULT_REQUEST_CAP)
    parser.add_argument("--time-cap-seconds", type=int, default=DEFAULT_TIME_CAP_SECONDS)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--chunk-request-budget", type=int, default=CHUNK_REQUEST_BUDGET)
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY, choices=range(1, 9))
    args = parser.parse_args()
    if args.command == "install":
        result = install()
    elif args.command == "start":
        result = start()
    elif args.command == "stop":
        result = stop()
    elif args.command == "status":
        result = status(database=args.database)
    elif args.command == "resume":
        result = resume()
    elif args.command == "uninstall":
        result = stop()
        AGENT_PATH.unlink(missing_ok=True)
        result["plist_removed"] = str(AGENT_PATH)
    else:
        code = _worker(database=args.database, request_cap=args.request_cap,
            time_cap_seconds=args.time_cap_seconds, batch_size=args.batch_size,
            chunk_request_budget=args.chunk_request_budget, concurrency=args.concurrency)
        raise SystemExit(code)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
