"""Single-operator, server-managed dashboard authentication.

The dashboard never receives a password hash or a reusable bearer token.  A
successful login creates an opaque session whose hash and CSRF token hash are
stored in the existing lifecycle database.  The existing bearer-token checks
remain available for machine-to-machine integrations.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

from outreach_agent.database import connect_database, is_postgres_target

SESSION_COOKIE = "geo_outreach_session"
CSRF_COOKIE = "geo_outreach_csrf"
SESSION_TTL_DAYS = 7
_PASSWORD_HASHER = PasswordHasher()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connect(path: str | os.PathLike[str]):
    db = connect_database(path)
    if not is_postgres_target(path):
        import sqlite3
        db.row_factory = sqlite3.Row
    return db


def init_auth_store(path: str | os.PathLike[str]) -> None:
    """Create only additive authentication tables in the existing registry."""
    with _connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS admin_sessions (
          session_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
          csrf_token_hash TEXT NOT NULL, email TEXT NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL, revoked_at TEXT, user_agent TEXT
        );
        CREATE INDEX IF NOT EXISTS admin_sessions_active_idx
          ON admin_sessions(token_hash, expires_at, revoked_at);
        CREATE TABLE IF NOT EXISTS admin_auth_events (
          event_id TEXT PRIMARY KEY, email TEXT, event_type TEXT NOT NULL,
          created_at TEXT NOT NULL, source_ip TEXT, detail TEXT
        );
        CREATE INDEX IF NOT EXISTS admin_auth_events_rate_idx
          ON admin_auth_events(email, event_type, created_at);
        """)


def configured_admin_email() -> str:
    email = os.environ.get("OUTREACH_ADMIN_EMAIL", "chen.yuxuan9@northeastern.edu").strip().casefold()
    parsed = parseaddr(email)[1]
    if not parsed or parsed != email or "@" not in email:
        raise RuntimeError("OUTREACH_ADMIN_EMAIL is not a valid email address")
    return email


def password_hash_configured() -> bool:
    value = os.environ.get("OUTREACH_ADMIN_PASSWORD_HASH", "").strip()
    return value.startswith("$argon2id$")


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("administrator password must be at least 12 characters")
    return _PASSWORD_HASHER.hash(password)


def _verify_password(password: str) -> bool:
    stored = os.environ.get("OUTREACH_ADMIN_PASSWORD_HASH", "").strip()
    if not stored.startswith("$argon2id$"):
        return False
    try:
        return bool(_PASSWORD_HASHER.verify(stored, password))
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def _write_event(db, email: str | None, event_type: str, source_ip: str, detail: str = "") -> None:
    db.execute(
        "INSERT INTO admin_auth_events(event_id,email,event_type,created_at,source_ip,detail) VALUES (?,?,?,?,?,?)",
        ("auth-" + secrets.token_urlsafe(12), email, event_type, _now(), source_ip[:128], detail[:240]),
    )


def _recent_failures(db, email: str, source_ip: str) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    row = db.execute(
        "SELECT COUNT(*) AS count FROM admin_auth_events WHERE event_type='LOGIN_FAILURE' AND created_at>=? AND (email=? OR source_ip=?)",
        (cutoff, email, source_ip[:128]),
    ).fetchone()
    return int(row["count"] if row else 0)


def authenticate(path: str | os.PathLike[str], email: str, password: str, source_ip: str = "") -> dict[str, Any] | None:
    init_auth_store(path)
    normalized = email.strip().casefold()
    with _connect(path) as db:
        if _recent_failures(db, normalized, source_ip) >= 10:
            _write_event(db, normalized, "LOGIN_RATE_LIMITED", source_ip)
            return None
        if normalized != configured_admin_email() or not _verify_password(password):
            _write_event(db, normalized, "LOGIN_FAILURE", source_ip)
            return None
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        created = datetime.now(timezone.utc)
        expires = created + timedelta(days=SESSION_TTL_DAYS)
        db.execute(
            "INSERT INTO admin_sessions(session_id,token_hash,csrf_token_hash,email,created_at,expires_at,last_seen_at,user_agent) VALUES (?,?,?,?,?,?,?,?)",
            ("session-" + secrets.token_urlsafe(12), _hash_token(token), _hash_token(csrf), normalized,
             created.isoformat(), expires.isoformat(), created.isoformat(), ""),
        )
        _write_event(db, normalized, "LOGIN_SUCCESS", source_ip)
        return {"token": token, "csrf": csrf, "expires_at": expires.isoformat(), "email": normalized}


def session(path: str | os.PathLike[str], token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    init_auth_store(path)
    with _connect(path) as db:
        row = db.execute("SELECT * FROM admin_sessions WHERE token_hash=? AND revoked_at IS NULL", (_hash_token(token),)).fetchone()
        if not row:
            return None
        try:
            if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
                return None
        except (TypeError, ValueError):
            return None
        db.execute("UPDATE admin_sessions SET last_seen_at=? WHERE session_id=?", (_now(), row["session_id"]))
        return dict(row)


def revoke(path: str | os.PathLike[str], token: str | None, source_ip: str = "") -> bool:
    current = session(path, token)
    if not current:
        return False
    with _connect(path) as db:
        db.execute("UPDATE admin_sessions SET revoked_at=? WHERE session_id=?", (_now(), current["session_id"]))
        _write_event(db, current["email"], "LOGOUT", source_ip)
    return True


def csrf_valid(current: dict[str, Any] | None, csrf: str | None) -> bool:
    if not current or not csrf:
        return False
    return hmac.compare_digest(current["csrf_token_hash"], _hash_token(csrf))


def cookie_header(name: str, value: str, *, secure: bool, max_age: int | None = None) -> str:
    parts = [f"{name}={value}", "Path=/", "HttpOnly" if name == SESSION_COOKIE else "", "SameSite=Strict"]
    if secure:
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(item for item in parts if item)


def clear_cookie_header(name: str, *, secure: bool) -> str:
    return cookie_header(name, "", secure=secure, max_age=0)
