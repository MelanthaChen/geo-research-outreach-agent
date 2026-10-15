import base64
import email
import json
import sys
import types
from email import policy
from pathlib import Path

import pytest

from outreach_agent.gmail_integration import (
    EmailEventStore, GmailSetupError, SCOPES, SERVICE_NAME, authorize_gmail,
    send_gmail_message,
)
from outreach_agent.email_test_settings import (
    AUTHORIZED_TEST_RECIPIENTS, PREFERRED_SENDER, is_public_https_form_url,
    load_test_email_settings, test_email_preview as build_test_email_preview, validate_test_recipient,
)

ROOT = Path(__file__).resolve().parents[1]


def test_sender_recipient_and_gmail_scope_configuration_is_exact():
    settings = load_test_email_settings()
    assert PREFERRED_SENDER == "yuxuan.chen.1031@gmail.com"
    assert settings["authorized_test_recipients"] == [
        "yuxaun.chen.1031@outlook.com", "yuxuan.chen.1031@outlook.com",
    ]
    assert AUTHORIZED_TEST_RECIPIENTS[0] != AUTHORIZED_TEST_RECIPIENTS[1]
    assert settings["gmail_scope"] == "https://www.googleapis.com/auth/gmail.send"
    assert settings["authenticated"] is False
    assert settings["send_enabled"] is False
    assert "Google OAuth desktop-client JSON" in settings["oauth_prerequisite"]
    assert "public HTTPS test participation form URL" in settings["prerequisite"]


def test_preview_contains_no_invalid_or_placeholder_as_working_form(tmp_path):
    default = build_test_email_preview(AUTHORIZED_TEST_RECIPIENTS[0])
    assert default["form_ready"] == "false"
    assert ".invalid" not in default["body"]
    assert "[PUBLIC HTTPS TEST FORM URL NOT CONFIGURED]" in default["body"]
    config = tmp_path / "test_email.yaml"
    config.write_text(
        "preferred_sender: yuxuan.chen.1031@gmail.com\n"
        "authorized_test_recipients:\n  - yuxaun.chen.1031@outlook.com\n  - yuxuan.chen.1031@outlook.com\n"
        "participation_form_url: https://research.example.org/test-interest\n"
        "participation_form_endpoint_verified: true\n",
        encoding="utf-8",
    )
    preview = build_test_email_preview(AUTHORIZED_TEST_RECIPIENTS[1], config)
    assert preview["form_ready"] == "true"
    assert preview["participation_form_url"] == "https://research.example.org/test-interest"
    assert preview["preview_hash"]
    assert "not a discovered company" in preview["body"]


def test_public_form_url_requires_https_and_rejects_reserved_or_local_hosts():
    assert is_public_https_form_url("https://forms.example.org/interest")
    assert not is_public_https_form_url("http://forms.example.org/interest")
    assert not is_public_https_form_url("https://research.example.invalid/form")
    assert not is_public_https_form_url("https://localhost/form")
    assert not is_public_https_form_url("https://user:pass@forms.example.org/form")


def test_backend_allowlist_is_exact_and_rejects_company_addresses():
    assert validate_test_recipient("yuxaun.chen.1031@outlook.com") == "yuxaun.chen.1031@outlook.com"
    assert validate_test_recipient("yuxuan.chen.1031@outlook.com") == "yuxuan.chen.1031@outlook.com"
    for value in ("hello@company.example", "yuxuan.chen.1031@outlook.com ", "Yuxaun.chen.1031@outlook.com"):
        with pytest.raises(ValueError):
            validate_test_recipient(value)


def test_event_store_idempotency_and_test_provenance(tmp_path):
    store = EmailEventStore(tmp_path / "events.sqlite3")
    preview_hash = "abc123"
    key = store.reserve(AUTHORIZED_TEST_RECIPIENTS[0], "Test subject", preview_hash)
    store.finish(key, "PROVIDER_ACCEPTED", message_id="gmail-id-1")
    with pytest.raises(ValueError, match="duplicate send blocked"):
        store.reserve(AUTHORIZED_TEST_RECIPIENTS[0], "Test subject", preview_hash)
    rows = store.recent()
    assert len(rows) == 1
    assert rows[0]["status"] == "PROVIDER_ACCEPTED"
    assert rows[0]["provider_message_id"] == "gmail-id-1"
    assert rows[0]["delivery_confirmation"] == "NOT_CONFIRMED"
    assert rows[0]["environment"] == "LIVE_TEST"


def test_gmail_api_acceptance_is_not_reported_as_delivery(monkeypatch):
    captured = {}

    class FakeCredentials:
        pass

    class FakeMessages:
        def send(self, *, userId, body):
            captured["user_id"] = userId
            captured["raw"] = body["raw"]
            return self
        def execute(self):
            return {"id": "api-message-123", "threadId": "thread-1"}

    class FakeUsers:
        def messages(self):
            return FakeMessages()

    class FakeService:
        def users(self):
            return FakeUsers()

    api_module = types.ModuleType("googleapiclient")
    api_module.__path__ = []
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda *args, **kwargs: FakeService()
    errors = types.ModuleType("googleapiclient.errors")
    errors.HttpError = type("HttpError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "googleapiclient", api_module)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)
    monkeypatch.setitem(sys.modules, "googleapiclient.errors", errors)
    monkeypatch.setattr("outreach_agent.gmail_integration._credentials", lambda settings=None: FakeCredentials())

    result = send_gmail_message(
        recipient=AUTHORIZED_TEST_RECIPIENTS[0], sender=PREFERRED_SENDER,
        subject="Controlled test", body="Only a test message.", settings={},
    )
    assert captured["user_id"] == "me"
    raw_message = email.message_from_bytes(base64.urlsafe_b64decode(captured["raw"]), policy=policy.default)
    assert raw_message["To"] == AUTHORIZED_TEST_RECIPIENTS[0]
    assert raw_message["From"] == PREFERRED_SENDER
    assert result == {
        "status": "PROVIDER_ACCEPTED", "provider_message_id": "api-message-123",
        "delivery_confirmation": "NOT_CONFIRMED",
    }


def test_oauth_rejects_wrong_google_account_without_saving_tokens(tmp_path, monkeypatch):
    client = tmp_path / "desktop-client.json"
    client.write_text(json.dumps({"installed": {"client_id": "desktop-id", "client_secret": "local-only"}}), encoding="utf-8")
    saved = {}

    class FakeCredentials:
        id_token = "identity-token"
        def to_json(self):
            return json.dumps({"token": "token-value"})

    class FakeFlow:
        client_config = {"client_id": "desktop-id"}
        @classmethod
        def from_client_secrets_file(cls, filename, scopes):
            assert scopes == SCOPES
            return cls()
        def run_local_server(self, **kwargs):
            assert kwargs["host"] == "127.0.0.1"
            assert kwargs["open_browser"] is True
            return FakeCredentials()

    class FakeKeyring:
        def set_password(self, *args):
            saved["value"] = args

    installed_flow = types.ModuleType("google_auth_oauthlib.flow")
    installed_flow.InstalledAppFlow = FakeFlow
    auth_oauthlib = types.ModuleType("google_auth_oauthlib")
    auth_oauthlib.__path__ = []
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", auth_oauthlib)
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", installed_flow)

    # Keep the test independent of actual Google libraries and network calls.
    google = types.ModuleType("google")
    google.__path__ = []
    auth_pkg = types.ModuleType("google.auth")
    auth_pkg.__path__ = []
    transport_pkg = types.ModuleType("google.auth.transport")
    transport_pkg.__path__ = []
    requests_pkg = types.ModuleType("google.auth.transport.requests")
    requests_pkg.Request = type("Request", (), {})
    oauth2_pkg = types.ModuleType("google.oauth2")
    oauth2_pkg.__path__ = []
    id_token_pkg = types.ModuleType("google.oauth2.id_token")
    id_token_pkg.verify_oauth2_token = lambda *args, **kwargs: {"email": "someoneelse@gmail.com", "email_verified": True}
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.auth", auth_pkg)
    monkeypatch.setitem(sys.modules, "google.auth.transport", transport_pkg)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", requests_pkg)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2_pkg)
    monkeypatch.setitem(sys.modules, "google.oauth2.id_token", id_token_pkg)
    monkeypatch.setattr("outreach_agent.gmail_integration._secure_keyring", lambda: FakeKeyring())
    settings = {"oauth_client_file": str(client)}

    with pytest.raises(GmailSetupError, match="not the configured preferred sender"):
        authorize_gmail(settings)
    assert saved == {}
