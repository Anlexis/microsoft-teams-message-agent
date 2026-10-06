"""AgentCore Platform v1.0 - Microsoft Graph (Teams messaging) client.

Service layer: a thin wrapper around the Microsoft Graph API v1.0 Teams
message endpoints. Contains NO business logic, NO routing, and NO credentials -
the integration token is passed in per call by the node (which reads it via
ctx.secrets). This module imports no framework/SDK internals - pure stdlib
(import-isolation, PB-4).

v1 LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented Microsoft Graph chatMessage response shape (an ``id`` +
    ``webUrl`` derived deterministically from the request) so the pipeline is
    runnable and testable without a live Microsoft 365 tenant or the
    ``requests`` package - it does NOT perform a live Graph call. The rule
    behind this shape: never fake a live call; document the limitation.

    To post real Teams messages, inject a live transport (requests-based
    ``post``) at construction time; the method contracts and payload shapes
    are already Microsoft Graph v1.0 exact
    (``POST /teams/{team-id}/channels/{channel-id}/messages`` and
    ``POST /chats/{chat-id}/messages``), so no business-logic change is
    needed to go live. A live transport also requires a real integration
    token (see CallTeamsApiNode - the stub runs without one because no
    request ever leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body) -> (status_code, response_dict)
Transport = Callable[[str, "dict[str, Any]", "dict[str, Any]"], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://graph.microsoft.com/v1.0"


class TeamsApiError(Exception):
    """Raised when the Microsoft Graph API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Microsoft Graph API error {status_code}: {message}")


class TeamsClient:
    """Microsoft Graph v1.0 Teams-message client.

    Args:
        base_url: Graph API base URL (default https://graph.microsoft.com/v1.0).
        post: optional injected transport (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE v1 stub is used
            (see the module docstring - it returns the documented chatMessage
            shape without a live Graph call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        post: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._post = post

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free v1 default)."""
        return self._post is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> dict[str, str]:
        """Build the Microsoft Graph auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_token,
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: "dict[str, Any]", json_body: "dict[str, Any]"
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free v1 stub - returns the documented Graph shape.

        NOT a live call. The synthetic message id is derived from the request
        so the response is stable and inspectable. See the module docstring
        for the v1 limitation and how to inject a live transport.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        # Graph message ids are epoch-millisecond-style numeric strings.
        message_id = str(int(digest[:12], 16))
        # Thread segment: the path element before the trailing /messages.
        parts = [p for p in url.split("/") if p]
        thread = parts[-2] if len(parts) >= 2 and parts[-1] == "messages" else "thread"
        # Documented chatMessage resource shape (subset).
        return 201, {
            "id": message_id,
            "messageType": "message",
            "createdDateTime": "1970-01-01T00:00:00Z",
            "webUrl": f"https://teams.microsoft.com/l/message/{thread}/{message_id}",
            "_stub": True,  # marks the network-free v1 stub response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def post_channel_message(
        self, team_id: str, channel_id: str, payload: "dict[str, Any]", api_token: str
    ) -> "dict[str, Any]":
        """POST /teams/{team-id}/channels/{channel-id}/messages - post a channel message.

        ``payload`` is the documented chatMessage request body
        (``{"body": {"contentType": ..., "content": ...}}``). Returns the
        parsed chatMessage response dict (containing ``id``/``webUrl``).
        Raises TeamsApiError on a non-2xx status.
        """
        url = f"{self._base_url}/teams/{team_id}/channels/{channel_id}/messages"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise TeamsApiError(status, _err_message(body))
        return body

    def post_chat_message(self, chat_id: str, payload: "dict[str, Any]", api_token: str) -> "dict[str, Any]":
        """POST /chats/{chat-id}/messages - post a 1:1 / group chat message.

        ``payload`` is the documented chatMessage request body. Returns the
        parsed chatMessage response dict (containing ``id``/``webUrl``).
        Raises TeamsApiError on a non-2xx status.
        """
        url = f"{self._base_url}/chats/{chat_id}/messages"
        transport = self._resolve(self._post)
        status, body = transport(url, self._headers(api_token), payload)
        if not (200 <= status < 300):
            raise TeamsApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a Graph error body."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code")
            if msg:
                return str(msg)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)
