"""Discover, verify, resume, and export OSM hotel properties independently."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from outreach_agent.hotels import (HOTEL_DB, HOTEL_PBF, export_hotel_cohort_membership, export_hotel_views, export_hotels, hotel_summary,
    export_geo_platform_profiles, export_startup_geo_profiles, hotel_batch_quality_pause, import_hotels,
    run_hotel_batch, select_hotel_pilot)

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.geo-research-outreach-agent.hotels"
AGENT_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
STATE_PATH = ROOT / "data/hotel_worker_state.json"
STOP_PATH = ROOT / "data/.hotel_stop.request"
LOCK_PATH = Path("/tmp/geo-research-hotel-pipeline.lock")
LOG_DIR = ROOT / "logs"
STDOUT_LOG = LOG_DIR / "hotel_worker.log"
STDERR_LOG = LOG_DIR / "hotel_worker.err.log"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(STATE_PATH)


def _launch_loaded() -> bool:
    return subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0


def _plist(database: Path, request_cap: int, time_cap: int, batch_size: int) -> dict[str, Any]:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-ims", sys.executable,
            str(ROOT / "scripts/phase7_hotels.py"), "worker", "--database", str(database),
            "--request-cap", str(request_cap), "--time-cap-seconds", str(time_cap),
            "--batch-size", str(batch_size)],
        "WorkingDirectory": str(ROOT), "EnvironmentVariables": {"PYTHONPATH": str(ROOT / "src")},
        "RunAtLoad": True, "KeepAlive": False, "ThrottleInterval": 60,
        "ProcessType": "Background", "StandardOutPath": str(STDOUT_LOG), "StandardErrorPath": str(STDERR_LOG),
    }


def install_worker(database: Path, request_cap: int, time_cap: int, batch_size: int) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise RuntimeError("The persistent hotel LaunchAgent is supported only on macOS")
    if _launch_loaded():
        return {"status": "already_loaded", "label": LABEL}
    AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    temp = AGENT_PATH.with_suffix(".plist.tmp")
    temp.write_bytes(plistlib.dumps(_plist(database, request_cap, time_cap, batch_size)))
    temp.replace(AGENT_PATH)
    result = subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(AGENT_PATH)], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    STOP_PATH.unlink(missing_ok=True)
    return {"status": "started", "label": LABEL, "plist": str(AGENT_PATH), "log": str(STDOUT_LOG)}


def resume_worker(request_increment: int, time_increment: int) -> dict[str, Any]:
    state = _read_state()
    pid = state.get("pid")
    if pid and subprocess.run(["/bin/kill", "-0", str(pid)], capture_output=True).returncode == 0:
        return {"status": "already_running", "pid": pid}
    old = {}
    if AGENT_PATH.exists():
        with AGENT_PATH.open("rb") as stream:
            old = plistlib.load(stream)
    args = old.get("ProgramArguments", [])
    def arg_int(flag: str, default: int) -> int:
        try:
            return int(args[args.index(flag) + 1])
        except (ValueError, IndexError):
            return default
    database = Path(args[args.index("--database") + 1]) if "--database" in args else HOTEL_DB
    batch = arg_int("--batch-size", 25)
    old_request_cap = arg_int("--request-cap", 1000)
    old_time_cap = arg_int("--time-cap-seconds", 21600)
    new_request_cap = old_request_cap + request_increment
    new_time_cap = old_time_cap + time_increment
    if _launch_loaded():
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False, capture_output=True)
    STOP_PATH.unlink(missing_ok=True)
    return install_worker(database, new_request_cap, new_time_cap, batch) | {
        "request_cap": new_request_cap, "time_cap_seconds": new_time_cap,
    }


def run_worker(database: Path, request_cap: int, time_cap: int, batch_size: int) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STOP_PATH.unlink(missing_ok=True)
    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Hotel database lock held; another hotel worker is already active", file=sys.stderr)
            return 2
        started = time.monotonic()
        state = _read_state()
        prior_attempts = int(state.get("http_attempts", 0))
        prior_elapsed = float(state.get("elapsed_seconds", 0))
        attempts = prior_attempts
        elapsed = prior_elapsed
        initial_progress = hotel_summary(database)
        _write_state({**state, "status": "running", "pid": os.getpid(), "started_at": _now(),
                      "http_attempts": attempts, "elapsed_seconds": elapsed, "last_error": None,
                      "stop_reason": None,
                      "processed": initial_progress["processed_properties"],
                      "verified": initial_progress["verified_properties"],
                      "geo_eligible": initial_progress["geo_eligible_properties"],
                      "remaining": initial_progress["pending_verification"]})
        try:
            while True:
                if STOP_PATH.exists():
                    reason = "operator_stop"
                    break
                current = hotel_summary(database)
                if current["pending_verification"] == 0:
                    reason = "candidate_pool_exhausted"
                    break
                if attempts >= request_cap:
                    reason = "request_cap_reached"
                    break
                if elapsed + (time.monotonic() - started) >= time_cap:
                    reason = "time_cap_reached"
                    break
                allowance = max(1, min(250, request_cap - attempts))
                batch_ids = select_hotel_pilot(database, min(batch_size, 25))
                if not batch_ids:
                    reason = "candidate_pool_exhausted"
                    break
                result = run_hotel_batch(len(batch_ids), database=database,
                    request_budget=allowance, time_budget_seconds=min(1800, max(1, int(time_cap - elapsed))),
                    concurrency=1, candidate_ids=batch_ids)
                result["sampling_strategy"] = "Deterministic round-robin of independent/unknown and chain-indicated properties, at most one per canonical domain, sorted by location and stable OSM ID"
                attempts += int(result.get("requests_attempted", 0))
                elapsed = prior_elapsed + (time.monotonic() - started)
                current = hotel_summary(database)
                exports = export_hotel_views(database)
                exports["geo_engine_hotel_profiles"] = str(export_geo_platform_profiles(database))
                exports["geo_engine_startup_profiles"] = str(export_startup_geo_profiles())
                _write_state({"status": "running", "pid": os.getpid(), "started_at": state.get("started_at", _now()),
                    "updated_at": _now(), "http_attempts": attempts, "elapsed_seconds": elapsed,
                    "processed": current["processed_properties"],
                    "verified": current["verified_properties"], "geo_eligible": current["geo_eligible_properties"],
                    "remaining": current["pending_verification"], "last_batch": result, "last_error": None})
                print(json.dumps({"progress": current, "batch": result, "exports": exports}, sort_keys=True), flush=True)
                if hotel_batch_quality_pause(result):
                    reason = "quality_degradation_pause_access_block_rate"
                    break
                if result.get("attempted", 0) == 0:
                    reason = "no_progress_or_budget_exhausted"
                    break
            elapsed = prior_elapsed + (time.monotonic() - started)
            _write_state({**_read_state(), "status": "stopped", "pid": None, "stop_reason": reason,
                "updated_at": _now(), "http_attempts": attempts, "elapsed_seconds": elapsed,
                "final": hotel_summary(database)})
            print(json.dumps({"status": "stopped", "stop_reason": reason, "summary": hotel_summary(database),
                              "http_attempts": attempts, "elapsed_seconds": elapsed}, sort_keys=True), flush=True)
            return 0
        except Exception as exc:
            _write_state({**_read_state(), "status": "failed", "pid": None,
                "last_error": f"{type(exc).__name__}: {exc}", "updated_at": _now()})
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover", help="Extract hotel-tagged property records from the local ODbL PBF")
    discover.add_argument("--pbf", type=Path, nargs="+", default=[HOTEL_PBF])
    discover.add_argument("--database", type=Path, default=HOTEL_DB)
    pilot = sub.add_parser("pilot", help="Run a bounded hotel verification pilot, at most 25 properties")
    pilot.add_argument("--limit", type=int, default=25)
    pilot.add_argument("--request-budget", type=int, default=100)
    pilot.add_argument("--database", type=Path, default=HOTEL_DB)
    export = sub.add_parser("export", help="Write a stable hotel candidate export")
    export.add_argument("--database", type=Path, default=HOTEL_DB)
    export.add_argument("--output", type=Path)
    cohorts = sub.add_parser("cohort-export", help="Export property-level BUSINESS/HOTEL membership without changing the live worker")
    cohorts.add_argument("--database", type=Path, default=HOTEL_DB)
    cohorts.add_argument("--output", type=Path)
    profiles = sub.add_parser("profiles", help="Export verified hotel and YC-attributed startup profiles for the GEO platform adapter")
    profiles.add_argument("--database", type=Path, default=HOTEL_DB)
    profiles.add_argument("--output", type=Path)
    profiles.add_argument("--startup-database", type=Path)
    profiles.add_argument("--startup-output", type=Path)
    status = sub.add_parser("status", help="Show hotel dataset, worker, and SQLite integrity state")
    status.add_argument("--database", type=Path, default=HOTEL_DB)
    run = sub.add_parser("run", help="Resume all unverified hotel candidates in bounded 25-property batches")
    run.add_argument("--database", type=Path, default=HOTEL_DB)
    run.add_argument("--batch-size", type=int, default=25)
    run.add_argument("--request-cap", type=int, default=1000)
    run.add_argument("--time-cap-seconds", type=int, default=21600)
    worker = sub.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("--database", type=Path, default=HOTEL_DB)
    worker.add_argument("--batch-size", type=int, default=25)
    worker.add_argument("--request-cap", type=int, default=1000)
    worker.add_argument("--time-cap-seconds", type=int, default=21600)
    install = sub.add_parser("install", help="Install and start an independent macOS hotel worker")
    install.add_argument("--database", type=Path, default=HOTEL_DB)
    install.add_argument("--batch-size", type=int, default=25)
    install.add_argument("--request-cap", type=int, default=1000)
    install.add_argument("--time-cap-seconds", type=int, default=21600)
    resume = sub.add_parser("resume", help="Resume the hotel worker with larger cumulative request/time caps")
    resume.add_argument("--request-increment", type=int, default=1000)
    resume.add_argument("--time-increment-seconds", type=int, default=21600)
    sub.add_parser("stop", help="Request a safe stop after the current 25-property batch")
    args = parser.parse_args()
    if args.command == "discover":
        results = [import_hotels(path, args.database) for path in args.pbf]
        print(json.dumps({"sources": results, "summary": hotel_summary(args.database)}, indent=2, sort_keys=True))
    elif args.command == "pilot":
        if not 1 <= args.limit <= 25:
            raise SystemExit("Pilot limit must be between 1 and 25")
        selected = select_hotel_pilot(args.database, args.limit)
        result = run_hotel_batch(len(selected), database=args.database, request_budget=args.request_budget,
            time_budget_seconds=1800, concurrency=1, candidate_ids=selected)
        result["selection_candidate_ids"] = selected
        result["sampling_strategy"] = "Deterministic round-robin of chain-indicated and unknown/independent properties, one per canonical domain, sorted by location and stable OSM property ID"
        with sqlite3.connect(args.database) as db:
            db.execute("UPDATE hotel_runs SET report_json=? WHERE run_id=(SELECT MAX(run_id) FROM hotel_runs)",
                       (json.dumps(result, sort_keys=True),))
        result["exports"] = export_hotel_views(args.database)
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "export":
        exports = export_hotel_views(args.database)
        if args.output:
            exports["all_candidates"] = str(export_hotels(args.database, args.output))
        print(json.dumps({"exports": exports, "rows": hotel_summary(args.database)["property_candidates"]}, indent=2))
    elif args.command == "cohort-export":
        path = export_hotel_cohort_membership(args.database, args.output or (ROOT / "data/phase7_hotel_cohort_membership.csv"))
        print(json.dumps({"hotel_cohort_membership_export": str(path), "rows": max(0, sum(1 for _ in path.open(encoding="utf-8-sig")) - 1)}, indent=2))
    elif args.command == "profiles":
        output = args.output or None
        path = export_geo_platform_profiles(args.database, output) if output else export_geo_platform_profiles(args.database)
        startup_path = (export_startup_geo_profiles(args.startup_database, args.startup_output)
                        if args.startup_database and args.startup_output else
                        export_startup_geo_profiles(args.startup_database) if args.startup_database else
                        export_startup_geo_profiles(output=args.startup_output) if args.startup_output else
                        export_startup_geo_profiles())
        print(json.dumps({"hotel_profile_export": str(path),
            "hotel_records": sum(1 for _ in path.open(encoding="utf-8")),
            "startup_profile_export": str(startup_path),
            "startup_records": sum(1 for _ in startup_path.open(encoding="utf-8"))}, indent=2))
    elif args.command == "status":
        with sqlite3.connect(args.database) as db:
            check = db.execute("PRAGMA integrity_check").fetchone()[0]
        state = _read_state()
        service = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], text=True, capture_output=True)
        pid = state.get("pid")
        running = bool(pid and subprocess.run(["/bin/kill", "-0", str(pid)], capture_output=True).returncode == 0)
        print(json.dumps({"summary": hotel_summary(args.database), "integrity": check, "worker_running": running,
            "launch_agent_loaded": service.returncode == 0, "pid": pid, "worker_state": state,
            "log": str(STDOUT_LOG), "error_log": str(STDERR_LOG)}, indent=2, sort_keys=True))
    elif args.command in {"run", "worker"}:
        if not 1 <= args.batch_size <= 25:
            raise SystemExit("Batch size must be between 1 and 25")
        run_worker(args.database, args.request_cap, args.time_cap_seconds, args.batch_size)
    elif args.command == "install":
        print(json.dumps(install_worker(args.database, args.request_cap, args.time_cap_seconds, args.batch_size), indent=2))
    elif args.command == "resume":
        print(json.dumps(resume_worker(args.request_increment, args.time_increment_seconds), indent=2))
    elif args.command == "stop":
        STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        STOP_PATH.write_text("stop after current batch\n", encoding="utf-8")
        print(json.dumps({"status": "stop_requested", "stop_file": str(STOP_PATH)}))


if __name__ == "__main__":
    main()
