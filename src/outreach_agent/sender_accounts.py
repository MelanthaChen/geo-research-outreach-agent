"""Registry-backed sender-account settings with fail-closed OAuth status."""

from __future__ import annotations

import re
import secrets
import sqlite3
from typing import Any

from outreach_agent.research_lifecycle import REGISTRY_DB, init_registry, now

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _schema(path):
    init_registry(path)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS sender_accounts (
          account_id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
          provider TEXT NOT NULL, reply_to TEXT, signature TEXT NOT NULL DEFAULT '',
          connection_status TEXT NOT NULL, is_default INTEGER NOT NULL DEFAULT 0,
          last_verified_at TEXT, reply_sync_status TEXT NOT NULL DEFAULT 'NOT_CONFIGURED',
          connection_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")


def list_accounts(path=REGISTRY_DB) -> list[dict[str, Any]]:
    _schema(path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM sender_accounts ORDER BY is_default DESC,email")]


def add_account(email: str, *, display_name: str = "", provider: str = "GMAIL", reply_to: str = "", signature: str = "", path=REGISTRY_DB) -> dict[str, Any]:
    if not EMAIL_RE.match(email.strip()):
        raise ValueError("sender email must be valid")
    if reply_to and not EMAIL_RE.match(reply_to.strip()):
        raise ValueError("reply-to must be valid")
    _schema(path); timestamp = now(); account_id = "sender-" + secrets.token_urlsafe(8)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO sender_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (account_id, email.strip().casefold(), display_name.strip(), provider.upper(), reply_to.strip().casefold(), signature, "NOT_CONNECTED", 0, None, "NOT_CONFIGURED", "OAuth connection is not configured", timestamp, timestamp))
    return next(item for item in list_accounts(path) if item["account_id"] == account_id)


def set_default(account_id: str, *, path=REGISTRY_DB) -> dict[str, Any]:
    _schema(path)
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT connection_status FROM sender_accounts WHERE account_id=?", (account_id,)).fetchone()
        if not row: raise KeyError(account_id)
        if row[0] != "CONNECTED": raise PermissionError("only a connected sender can be selected as default")
        db.execute("UPDATE sender_accounts SET is_default=0,updated_at=?", (now(),)); db.execute("UPDATE sender_accounts SET is_default=1,updated_at=? WHERE account_id=?", (now(), account_id))
    return next(item for item in list_accounts(path) if item["account_id"] == account_id)


def disconnect(account_id: str, *, path=REGISTRY_DB) -> dict[str, Any]:
    _schema(path)
    with sqlite3.connect(path) as db:
        changed = db.execute("UPDATE sender_accounts SET connection_status='DISCONNECTED',is_default=0,reply_sync_status='STOPPED',connection_error='Disconnected by administrator',updated_at=? WHERE account_id=?", (now(), account_id)).rowcount
        if not changed: raise KeyError(account_id)
    return next(item for item in list_accounts(path) if item["account_id"] == account_id)

