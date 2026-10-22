"""Microsoft Graph delegated provider; authentication is intentionally opt-in.

The provider accepts an injected token/transport for tests. Creating an MSAL
client or starting interactive login is separate and never happens on import.
"""
from __future__ import annotations

from typing import Callable, Any
import httpx

from outreach_agent.email_dispatch import EmailProviderError, ProviderResult

GRAPH_SEND_SCOPE = "https://graph.microsoft.com/Mail.Send"
DELEGATED_SCOPES = ("openid", "profile", "email", "offline_access", GRAPH_SEND_SCOPE)
GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"


def authorize_delegated(*, client_id: str, tenant: str = "common", scopes: tuple[str, ...] = DELEGATED_SCOPES) -> dict[str, Any]:
    """Start delegated MSAL login only when explicitly called by an operator.

    The optional ``msal`` package is deliberately not imported at module load.
    This function is not called by the dashboard, tests, or dispatch pipeline.
    Production setup must register an Entra public-client application and use
    its client ID; the resulting token must be used only with
    :class:`MicrosoftGraphProvider` after sender verification.
    """
    try:
        import msal  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Microsoft Graph authentication requires the optional 'msal' dependency") from exc
    app = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant}")
    result = app.acquire_token_interactive(scopes=list(scopes), prompt="select_account")
    if "access_token" not in result:
        raise RuntimeError(str(result.get("error_description") or result.get("error") or "Microsoft authentication failed"))
    return result


def verify_sender_identity(claims: dict[str, Any], expected_sender: str) -> bool:
    expected = expected_sender.casefold().strip()
    values = {str(claims.get(key) or "").casefold().strip() for key in ("email", "preferred_username", "upn")}
    return bool(expected and expected in values)


class MicrosoftGraphProvider:
    name = "microsoft_graph"

    def __init__(self, *, access_token: str, authenticated_sender: str, transport: Callable[..., Any] | None = None, enabled: bool = True):
        if not access_token or not authenticated_sender:
            raise ValueError("Graph provider requires an access token and verified sender")
        self.access_token = access_token
        self.authenticated_sender = authenticated_sender.casefold().strip()
        self.transport = transport or httpx.post
        self.enabled = enabled

    def send(self, *, sender: str, recipient: str, subject: str, body: str) -> ProviderResult:
        if sender.casefold().strip() != self.authenticated_sender:
            raise EmailProviderError("sender identity does not match authenticated Graph account")
        response = self.transport(GRAPH_SEND_URL, headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}, json={"message": {"subject": subject, "body": {"contentType": "Text", "content": body}, "toRecipients": [{"emailAddress": {"address": recipient}}]}, "saveToSentItems": True})
        status = int(getattr(response, "status_code", 0))
        if status not in (200, 202):
            retryable = status == 429 or status >= 500 or status == 0
            raise EmailProviderError(f"Microsoft Graph rejected message (HTTP {status})", retryable=retryable)
        message_id = (getattr(response, "headers", {}) or {}).get("request-id") or "graph-accepted"
        return ProviderResult(message_id=message_id)
