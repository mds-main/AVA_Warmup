"""Minimal Genesys Cloud Conversations API client.

Translates Web Messaging guest ``messageId`` values into the real Genesys
``conversationId`` (a.k.a. interactionId) via
``GET /api/v2/conversations/messages/{messageId}/details``. The guest
WebSocket protocol does not expose conversationId directly, so the operator
must provide OAuth client-credentials and we look it up post-hoc.

References:
- https://developer.genesys.cloud/api/digital/webmessaging/websocketapi
- https://developer.genesys.cloud/authorization/platform-auth/use-client-credentials
- https://developer.genesys.cloud/routing/conversations/conversations-apis
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


class GenesysApiError(Exception):
    """Raised when the Conversations REST API lookup fails."""


class GenesysApiClient:
    """OAuth-client-credentials Conversations API client.

    Token cache is process-local and protected by a lock. Tokens are refreshed
    a few seconds before their reported expiry to avoid edge races.
    """

    _TOKEN_REFRESH_MARGIN_SECONDS = 30.0
    _REQUEST_TIMEOUT_SECONDS = 10.0

    def __init__(self, *, client_id: str, client_secret: str, region: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.region = region
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def login_base_url(self) -> str:
        return f"https://login.{self.region}"

    @property
    def api_base_url(self) -> str:
        return f"https://api.{self.region}"

    def _ensure_token(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at - self._TOKEN_REFRESH_MARGIN_SECONDS:
                return self._token
            credentials = f"{self.client_id}:{self.client_secret}".encode("utf-8")
            authorization = "Basic " + base64.b64encode(credentials).decode("ascii")
            body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8")
            request = urllib.request.Request(
                url=f"{self.login_base_url}/oauth/token",
                data=body,
                method="POST",
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self._REQUEST_TIMEOUT_SECONDS) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise GenesysApiError(
                    f"OAuth token request failed: HTTP {exc.code} from {self.login_base_url}/oauth/token: {detail}"
                ) from exc
            except urllib.error.URLError as exc:
                raise GenesysApiError(
                    f"OAuth token request failed to reach {self.login_base_url}: {exc.reason}"
                ) from exc
            access_token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not isinstance(access_token, str) or not access_token:
                raise GenesysApiError("OAuth token response missing access_token.")
            self._token = access_token
            self._token_expires_at = now + float(expires_in or 0.0)
            return access_token

    def get_conversation_id_for_message(self, message_id: str) -> dict[str, Any]:
        """Return ``{"conversation_id": ..., "raw": <full response body>}``.

        Raises :class:`GenesysApiError` on auth or API failures.
        """

        normalized = str(message_id or "").strip()
        if not normalized:
            raise GenesysApiError("messageId is required.")
        token = self._ensure_token()
        request = urllib.request.Request(
            url=f"{self.api_base_url}/api/v2/conversations/messages/{urllib.parse.quote(normalized)}/details",
            method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GenesysApiError(
                f"Conversations API returned HTTP {exc.code} for messageId={normalized}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise GenesysApiError(
                f"Conversations API request failed: {exc.reason}"
            ) from exc
        conversation_id = (
            payload.get("conversationId")
            or payload.get("conversation_id")
            or (payload.get("conversation") or {}).get("id")
            if isinstance(payload, dict)
            else None
        )
        return {"conversation_id": conversation_id, "raw": payload}
