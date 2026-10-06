"""AgentCore Platform v1.0 - inner workflow Step 3: InferTeamsFields.

Extracts the message body and the explicit Teams target (team / channel /
chat ids) from the (redacted) request and assembles a validated Microsoft
Graph chatMessage request body for the classified intent. A target id is
taken only from an explicit id in the text, the caller-supplied target_hint,
or an id-shaped "Key: value" line - an unresolved target is left empty rather
than invented (risk mitigation: never post to the wrong channel or chat; the
executor surfaces the miss as status=error). The message body is likewise
explicit-only (quoted after a messaging verb, or a "Message:" line) - noise
is never posted. Deterministic v1 - no LLM (docs/02_design.md "v1
Implementation Note").
"""

import re

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json
from src.services.security import TEAMS_ID_RE

# A Teams target id: GUID (team) or thread-style id like 19:...@thread.tacv2
# (channel / chat) - alphanumeric plus :@._- (no spaces). Single definition in
# src/services/security.py - the same bounded shape pre_process validates
# caller-supplied hints against.
_ID_SHAPE_RE = TEAMS_ID_RE
_ID_CHARS = r"[A-Za-z0-9][A-Za-z0-9:@._-]{0,79}"
# Explicit id mentions in the request text, EN or JA
# ("team id a1b2..." / "channel id: 19:...@thread.tacv2" / "チームID a1b2...").
_TEAM_IN_TEXT_RE = re.compile(
    rf"team\s+id\s*[:#]?\s*({_ID_CHARS})|チーム\s*ID\s*[:：#]?\s*({_ID_CHARS})",
    re.IGNORECASE,
)
_CHANNEL_IN_TEXT_RE = re.compile(
    rf"channel\s+id\s*[:#]?\s*({_ID_CHARS})|チャン?ネル\s*ID\s*[:：#]?\s*({_ID_CHARS})",
    re.IGNORECASE,
)
_CHAT_IN_TEXT_RE = re.compile(
    rf"chat\s+id\s*[:#]?\s*({_ID_CHARS})|チャット\s*ID\s*[:：#]?\s*({_ID_CHARS})",
    re.IGNORECASE,
)
# Quoted message body after a messaging verb: saying "Foo" / post: "Foo".
# Curly quotes as literal alternatives so pasted rich-text requests work.
_MSG_QUOTED_RE = re.compile(
    r"(?:saying|says|say|sending|send|posting|post|announce|write|message)" r'\s*[:,]?\s*["“]([^"”\n]+)["”]',
    re.IGNORECASE,
)
# "Key: value" lines (ASCII or full-width colon). CJK ranges as \u escapes
# stay out - hiragana/katakana + CJK unified ideographs written directly is
# avoided; ASCII + explicit JA key literals below cover the v1 scope.
_KV_RE = re.compile(r"^\s*([A-Za-z][\w \-]{0,40}|本文|メッセージ)[:：]\s*(.+?)\s*$")
# KV keys that carry the message body vs. a target id.
_MESSAGE_KEYS = ("message", "body", "text", "本文", "メッセージ")
_TEAM_KEYS = ("team id", "team")
_CHANNEL_KEYS = ("channel id", "channel")
_CHAT_KEYS = ("chat id", "chat")

_PREVIEW_LEN = 80


class InferTeamsFieldsNode(FunctionNode):
    """Extract entities and assemble the Microsoft Graph chatMessage request body."""

    # Inner domain node - derives fields from already-validated text; the
    # external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "post_channel_message") or "post_channel_message"
        target_hint = from_json(state.get("target_hint"), {}) or {}

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferTeamsFieldsNode: missing validated_input"],
            }

        fields = self._parse_fields(text)
        team_id = self._resolve_id(_TEAM_IN_TEXT_RE, text, target_hint.get("team_id", ""), _TEAM_KEYS, fields)
        channel_id = self._resolve_id(
            _CHANNEL_IN_TEXT_RE, text, target_hint.get("channel_id", ""), _CHANNEL_KEYS, fields
        )
        chat_id = self._resolve_id(_CHAT_IN_TEXT_RE, text, target_hint.get("chat_id", ""), _CHAT_KEYS, fields)
        message = self._resolve_message(text, fields)

        # Microsoft Graph chatMessage request body (targets ride as URL path
        # params in the client call, not in the body).
        payload = {"body": {"contentType": "text", "content": message}}

        # Audit the assembled payload shape - presence signals only, not content.
        emit_trace_event(
            "infer_teams_fields_complete",
            {
                "intent": intent,
                "has_team_id": bool(team_id),
                "has_channel_id": bool(channel_id),
                "has_chat_id": bool(chat_id),
                "has_message": bool(message),
            },
            state,
        )

        return {
            "team_id": team_id,
            "channel_id": channel_id,
            "chat_id": chat_id,
            "message_preview": message[:_PREVIEW_LEN],
            "teams_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_id(
        self, pattern: "re.Pattern[str]", text: str, hint: str, keys: "tuple[str, ...]", fields: "list[tuple[str, str]]"
    ) -> str:
        """Explicit id only: text mention > id-shaped hint > id-shaped KV field. Never invented."""
        m = pattern.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = str(hint or "").strip()
        if hint and _ID_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in keys and _ID_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_message(self, text: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit body only: quoted-after-verb > 'Message:' line. Never the whole text."""
        m = _MSG_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()
        for key, value in fields:
            if key.strip().lower() in _MESSAGE_KEYS:
                return value.strip()
        return ""  # unresolved - the executor refuses to post an empty body

    def _parse_fields(self, text: str) -> "list[tuple[str, str]]":
        """Return the [(key, value), ...] pairs parsed from the request lines."""
        fields: list[tuple[str, str]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                fields.append((m.group(1).strip(), m.group(2).strip()))
        return fields
