"""Fail-closed configuration for the isolated Gmail test-email workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json

import yaml

from outreach_agent.gmail_integration import gmail_connection_status
from outreach_agent.outreach_pipeline import mail_merge_safe_subject

ROOT = Path(__file__).resolve().parents[2]
TEST_EMAIL_CONFIG = ROOT / "config/test_email.yaml"
PREFERRED_SENDER = "yuxuan.chen.1031@gmail.com"
AUTHORIZED_TEST_RECIPIENTS = (
    "yuxaun.chen.1031@outlook.com",
    "yuxuan.chen.1031@outlook.com",
)
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def load_test_email_settings(path: Path = TEST_EMAIL_CONFIG) -> dict[str, Any]:
    """Read persistent configuration and ensure the hard allowlist cannot drift."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    recipients = tuple(loaded.get("authorized_test_recipients") or ())
    sender = str(loaded.get("preferred_sender") or "")
    if sender != PREFERRED_SENDER:
        raise ValueError("Configured preferred sender does not match the approved test sender")
    if recipients != AUTHORIZED_TEST_RECIPIENTS:
        raise ValueError("Configured test recipients do not match the exact backend allowlist")
    auth = gmail_connection_status(loaded)
    form_url = str(loaded.get("participation_form_url") or "").strip()
    form_url_valid = is_public_https_form_url(form_url)
    form_ready = form_url_valid and loaded.get("participation_form_endpoint_verified") is True
    prerequisites = []
    if not auth["authenticated"]:
        prerequisites.append(auth["prerequisite"])
    if not form_url_valid:
        prerequisites.append("A deployed public HTTPS test participation form URL is required; the reserved .invalid demo URL is not valid.")
    elif not form_ready:
        prerequisites.append("The HTTPS form endpoint has not been marked verified for token expiry/revocation, server-side validation, rate limits, and isolated test-submission storage.")
    return {
        "preferred_sender": sender,
        "authorized_test_recipients": list(AUTHORIZED_TEST_RECIPIENTS),
        "provider": "Gmail API",
        "authenticated": auth["authenticated"],
        "authenticated_sender": auth.get("authenticated_sender", ""),
        "oauth_client_configured": auth["oauth_client_configured"],
        "oauth_client_file": auth["oauth_client_file"],
        "gmail_scope": GMAIL_SCOPE,
        "participation_form_url": form_url,
        "participation_form_url_valid": form_url_valid,
        "participation_form_ready": form_ready,
        "send_enabled": bool(loaded.get("test_sending_enabled")) and auth["authenticated"] and form_ready,
        "prerequisite": " ".join(prerequisites) if prerequisites else "",
        "oauth_prerequisite": auth["prerequisite"],
    }


def validate_test_recipient(recipient: str) -> str:
    """Validate an exact, explicitly authorized test recipient."""
    if not isinstance(recipient, str) or recipient not in AUTHORIZED_TEST_RECIPIENTS:
        raise ValueError("Recipient is not in the exact test-email allowlist")
    return recipient


def is_public_https_form_url(value: str) -> bool:
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(value)
    hostname = parsed.hostname or ""
    try:
        if hostname:
            address = ipaddress.ip_address(hostname)
            if not address.is_global:
                return False
    except ValueError:
        pass
    return bool(
        parsed.scheme == "https" and hostname
        and not hostname.endswith(".invalid")
        and hostname not in {"localhost", "127.0.0.1", "::1"}
        and not parsed.username and not parsed.password
    )


def test_email_preview(recipient: str, path: Path = TEST_EMAIL_CONFIG) -> dict[str, str]:
    """Build the exact controlled invitation preview without substituting company contacts."""
    address = validate_test_recipient(recipient)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    form_url = str(loaded.get("participation_form_url") or "").strip()
    form_url_valid = is_public_https_form_url(form_url)
    visible_form_url = form_url if form_url_valid else "[PUBLIC HTTPS TEST FORM URL NOT CONFIGURED]"
    form_ready = form_url_valid and loaded.get("participation_form_endpoint_verified") is True
    sender = str(loaded.get("preferred_sender") or PREFERRED_SENDER)
    message = {
        "from": sender,
        "to": address,
        "subject": mail_merge_safe_subject("GEO Research Outreach — Test Participation Invitation"),
        "body": (
            "Hello,\n\n"
            "This is a controlled test of the GEO research invitation and participation-form workflow. "
            "It is addressed to an authorized test recipient, not a discovered company.\n\n"
            "We are testing the research invitation workflow about Generative Engine Optimization (GEO) "
            "and visibility in AI-powered search. To test the interest-form handoff, open the test form below:\n\n"
            f"{visible_form_url}\n\n"
            "Any submission through the configured test form is test data only, is not formal research consent, "
            "and does not enroll a participant. Please do not forward this test message.\n\n"
            f"Questions about this test can be sent to {sender}.\n\n"
            "GEO Research Outreach test workflow"
        ),
        "participation_form_url": visible_form_url,
        "form_ready": "true" if form_ready else "false",
        "status": "TEST PREVIEW · NOT SENT",
    }
    canonical = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    message["preview_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return message
