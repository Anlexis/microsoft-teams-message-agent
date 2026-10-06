"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-request input, refuses instruction-override content on
BOTH arrival channels (the structured JSON the outer graph serialized, and the
legacy bare-string form), and runs a deterministic (regex, NOT LLM) scan of
the inbound text for email addresses / access-token-like strings, which are
flag-and-redacted before anything is logged. A Teams message request
legitimately names people and targets (the framework input-side PII mask in
BaseNode.__call__ additionally masks emails/phones/names in user_input /
validated_input), so the PII scan is flag-and-redact for safe logging, not a
hard reject. The deterministic auto-rejects are the empty / non-request guard
and the instruction-override screen.
"""

import json
import re

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json, to_json
from src.services.security import find_instruction_override

# Deterministic redaction patterns: email addresses and bearer/JWT/API-token-like
# strings that might appear in a pasted request. Flagged + redacted before logging.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")
_TOKEN_RE = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,})\b")
_REDACTION = "[REDACTED]"

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3

# The serialized-request field names the outer contract defines - the only
# path components the instruction-override refusal may echo. Anything else in
# the decoded request is caller-controlled text and is masked.
_ECHOABLE_FIELDS = frozenset({"text", "target_hint", "team_id", "channel_id", "chat_id"})


class ValidateInputNode(FunctionNode):
    """Validate + flag-and-redact the inbound Teams message request."""

    # Inner domain node - the external gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        # Target-hint precedence: the STATE field wins over the copy embedded
        # in the serialized request - the state field arrives unmasked via the
        # graph bridge (context_bridge.py), while the framework's PII mask can
        # rewrite ids inside validated_input (a real chat id matches the email
        # pattern), so the embedded copy is only a fallback for direct runs.
        text = raw
        decoded: Any = raw
        state_hint = from_json(state.get("target_hint"), {}) or {}
        embedded_hint: dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                embedded_hint = obj.get("target_hint", {}) or {}
                decoded = obj
            except (ValueError, TypeError):
                text = raw
        target_hint = state_hint or embedded_hint

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # ── Instruction-override screen (template-owned, fail CLOSED) ─────────
        # The outer PreProcessNode refuses this content at the contract
        # boundary; this node repeats the refusal at the INNER boundary so
        # driving the inner graph directly gives the same guarantee on both
        # channels - the structured serialized request (decoded here, every
        # string at any depth, keys included) and the legacy bare string. A hit
        # errors with NOTHING carried forward: no validated_input, no
        # target_hint, no redaction_flags. The echoed path masks
        # caller-controlled field names; the hostile string never appears.
        flagged_path = find_instruction_override(decoded, known_fields=_ECHOABLE_FIELDS, root="request")
        if flagged_path is None and state_hint:
            flagged_path = find_instruction_override(state_hint, known_fields=_ECHOABLE_FIELDS, root="target_hint")
        if flagged_path is not None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ValidateInputNode: {flagged_path} refused - instruction-override directive"],
            }

        # Deterministic flag-and-redact (before any logging).
        # Local list per invocation - never a module-global (no cross-invoke leak).
        flags: list[str] = []
        redacted = text
        if _EMAIL_RE.search(redacted):
            flags.append("email")
            redacted = _EMAIL_RE.sub(_REDACTION, redacted)
        if _TOKEN_RE.search(redacted):
            flags.append("token")
            redacted = _TOKEN_RE.sub(_REDACTION, redacted)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_target_hint": bool(target_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "target_hint": to_json(target_hint) if target_hint else None,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
