"""Narrow Gmail OAuth/send integration for the two-address test workflow.

OAuth client configuration stays outside the repository. OAuth tokens are kept
only in an OS-backed keyring (macOS Keychain, Windows Credential Locker, etc.).
The module never sends automatically; callers must first present a reviewed
preview and submit explicit confirmation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from email.message import EmailMessage
from pathlib import Path
from typing import Any

SERVICE_NAME = "geo-research-outreach-agent-gmail"
SCOPES = ["https://www.googleapis.com/auth/gmail.send", "openid", "email"]
DEFAULT_CLIENT_FILE = Path.home() / ".config/geo-research-outreach-agent/gmail-oauth-client.json"
DEFAULT_EVENT_DB = Path.home() / ".local/share/geo-research-outreach-agent/test-email-events.sqlite3"


class GmailSetupError(RuntimeError):
    pass


class GmailProviderError(RuntimeError):
    def __init__(self, message: str, *, provider_rejected: bool = False):
        super().__init__(message)
        self.provider_rejected = provider_rejected


def _client_file(settings: dict[str, Any] | None = None) -> Path:
    configured = (settings or {}).get("oauth_client_file") or str(DEFAULT_CLIENT_FILE)
    return Path(os.path.expanduser(os.environ.get("GEO_OUTREACH_GOOGLE_OAUTH_CLIENT_FILE", configured)))


def _secure_keyring():
    try:
        import keyring
    except ImportError as exc:
        raise GmailSetupError("Gmail dependencies are missing. Install the optional 'gmail' extra.") from exc
    backend = keyring.get_keyring()
    if getattr(backend, "priority", 0) <= 0 or "fail" in type(backend).__module__.casefold():
        raise GmailSetupError("No secure OS credential store is available; Gmail tokens were not saved.")
    return keyring


def _credential_record(settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    from outreach_agent.email_test_settings import PREFERRED_SENDER

    keyring = _secure_keyring()
    raw = keyring.get_password(SERVICE_NAME, PREFERRED_SENDER)
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GmailSetupError("Saved Gmail keychain entry is invalid; reconnect Gmail.") from exc
    if record.get("email") != PREFERRED_SENDER:
        raise GmailSetupError("Saved Google account does not match the configured Gmail sender.")
    return record


def gmail_connection_status(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Report local OAuth readiness without triggering browser or network access."""
    client_file = _client_file(settings)
    result: dict[str, Any] = {
        "authenticated": False,
        "authenticated_sender": "",
        "oauth_client_configured": client_file.is_file(),
        "oauth_client_file": str(client_file),
        "prerequisite": "",
    }
    if not client_file.is_file():
        result["prerequisite"] = (
            "Missing Google OAuth desktop-client JSON. Enable Gmail API, configure the consent screen, "
            "create a Desktop app OAuth client, and place its JSON at the configured path outside the repository."
        )
        return result
    try:
        record = _credential_record(settings)
    except GmailSetupError as exc:
        result["prerequisite"] = str(exc)
        return result
    if record:
        try:
            from google.oauth2.credentials import Credentials

            credentials = Credentials.from_authorized_user_info(record["credentials"], SCOPES)
            usable = bool(credentials.valid or credentials.refresh_token)
        except (ImportError, ValueError, KeyError, TypeError):
            usable = False
        result["authenticated"] = usable
        result["authenticated_sender"] = str(record["email"]) if usable else ""
        if not usable:
            result["prerequisite"] = "Saved Gmail authorization is invalid or expired. Reconnect Gmail."
        return result
    try:
        _secure_keyring()
    except GmailSetupError as exc:
        result["prerequisite"] = str(exc)
        return result
    result["prerequisite"] = "Gmail is not connected. Use Connect Gmail and authorize the configured account."
    return result


def authorize_gmail(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the installed-app browser OAuth flow, then store tokens in OS keyring."""
    from outreach_agent.email_test_settings import PREFERRED_SENDER

    client_file = _client_file(settings)
    if not client_file.is_file():
        raise GmailSetupError(gmail_connection_status(settings)["prerequisite"])
    try:
        raw_client = json.loads(client_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GmailSetupError("Google OAuth client JSON cannot be read.") from exc
    if "installed" not in raw_client:
        raise GmailSetupError("OAuth client JSON must be for an installed/desktop application, not a web client.")
    keyring = _secure_keyring()
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GmailSetupError("Gmail dependencies are missing. Install the optional 'gmail' extra.") from exc
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=True,
        authorization_prompt_message="Opening Google sign-in in your browser…",
        authorization_url_kwargs={"access_type": "offline", "prompt": "consent select_account"},
    )
    if not credentials.id_token:
        raise GmailSetupError("Google did not return an identity token; account verification failed.")
    claims = id_token.verify_oauth2_token(credentials.id_token, Request(), audience=flow.client_config["client_id"])
    authenticated_email = str(claims.get("email") or "").casefold()
    if authenticated_email != PREFERRED_SENDER.casefold() or claims.get("email_verified") is not True:
        raise GmailSetupError("Signed-in Google account is not the configured preferred sender; token was not saved.")
    record = {"email": PREFERRED_SENDER, "credentials": json.loads(credentials.to_json())}
    keyring.set_password(SERVICE_NAME, PREFERRED_SENDER, json.dumps(record, separators=(",", ":")))
    return gmail_connection_status(settings)


def disconnect_gmail() -> None:
    from outreach_agent.email_test_settings import PREFERRED_SENDER

    _secure_keyring().delete_password(SERVICE_NAME, PREFERRED_SENDER)


def _credentials(settings: dict[str, Any] | None = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from outreach_agent.email_test_settings import PREFERRED_SENDER

    record = _credential_record(settings)
    if not record:
        raise GmailSetupError(gmail_connection_status(settings)["prerequisite"])
    credentials = Credentials.from_authorized_user_info(record["credentials"], SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        record["credentials"] = json.loads(credentials.to_json())
        _secure_keyring().set_password(SERVICE_NAME, PREFERRED_SENDER, json.dumps(record, separators=(",", ":")))
    if not credentials.valid:
        raise GmailSetupError("Gmail authorization expired; reconnect Gmail before sending.")
    return credentials


def send_gmail_message(*, recipient: str, sender: str, subject: str, body: str,
                       settings: dict[str, Any] | None = None) -> dict[str, str]:
    """Send one reviewed message via Gmail API; returned acceptance is not delivery."""
    from outreach_agent.email_test_settings import PREFERRED_SENDER, validate_test_recipient

    validate_test_recipient(recipient)
    if sender != PREFERRED_SENDER:
        raise GmailSetupError("Message sender does not match the configured Gmail account.")
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        raise GmailSetupError("Gmail dependencies are missing. Install the optional 'gmail' extra.") from exc
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = PREFERRED_SENDER
    message["Subject"] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        service = build("gmail", "v1", credentials=_credentials(settings), cache_discovery=False)
        response = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", "unknown")
        try:
            rejected = 400 <= int(status) < 500
        except (TypeError, ValueError):
            rejected = False
        message = (f"Gmail API rejected the request (HTTP {status})." if rejected
                   else f"Gmail API response was ambiguous (HTTP {status}); acceptance is unknown.")
        raise GmailProviderError(message, provider_rejected=rejected) from exc
    except GmailSetupError:
        raise
    except Exception as exc:  # transport failures may have an ambiguous send outcome
        raise GmailProviderError("Gmail request failed; acceptance is unknown. Inspect Gmail before any retry.") from exc
    message_id = str(response.get("id") or "")
    if not message_id:
        raise GmailProviderError("Gmail returned no message ID; acceptance is unknown.")
    return {"status": "PROVIDER_ACCEPTED", "provider_message_id": message_id,
            "delivery_confirmation": "NOT_CONFIRMED"}


class EmailEventStore:
    """Separate local audit for controlled test sends; never writes company history."""

    def __init__(self, path: Path = DEFAULT_EVENT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS test_email_events (
                id INTEGER PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                recipient TEXT NOT NULL, subject TEXT NOT NULL, preview_hash TEXT NOT NULL,
                status TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'GMAIL_API',
                provider_message_id TEXT NOT NULL DEFAULT '', delivery_confirmation TEXT NOT NULL DEFAULT 'NOT_CONFIRMED',
                error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                environment TEXT NOT NULL DEFAULT 'LIVE_TEST' CHECK(environment='LIVE_TEST'))""")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def key(recipient: str, subject: str, body: str) -> str:
        payload = f"{recipient}\0{subject}\0{body}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def reserve(self, recipient: str, subject: str, preview_hash: str) -> str:
        key = self.key(recipient, subject, preview_hash)
        try:
            with self.connect() as db:
                db.execute("INSERT INTO test_email_events(idempotency_key,recipient,subject,preview_hash,status) VALUES(?,?,?,?,?)",
                           (key, recipient, subject, preview_hash, "SEND_REQUESTED"))
        except sqlite3.IntegrityError as exc:
            raise ValueError("This exact test message already has a send attempt; duplicate send blocked.") from exc
        return key

    def finish(self, key: str, status: str, *, message_id: str = "", error: str = "") -> None:
        with self.connect() as db:
            db.execute("UPDATE test_email_events SET status=?,provider_message_id=?,error=?,delivery_confirmation='NOT_CONFIRMED',updated_at=CURRENT_TIMESTAMP WHERE idempotency_key=?",
                       (status, message_id, error[:500], key))

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM test_email_events ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 100),))]
