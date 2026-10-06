"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: serialize the caller's request (raw NL text + Teams
target hint) into a single JSON string in `validated_input`, which the GraphNode
(`main` slot) hands to the inner Teams workflow graph. Business validation
happens inside the inner graph's ValidateInputNode - this node owns the
CALLER-DATA CONTRACT: the empty-guard, the template-owned instruction-override
refusal, the HTML/length sanitize, and the field-by-field validation of
`input_context` (every accepted value is bounded; a malformed value is refused
with an error that names the field and never echoes the value).
"""

import json

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import (
    INSTRUCTION_OVERRIDE_RE,
    TEAMS_ID_RE,
    find_instruction_override,
    sanitize_query,
)

# input_context keys that may carry an explicit Teams target (caller-supplied
# only - a target is never inferred here). Unknown keys are ignored, but their
# strings are still screened for instruction-override content below - and only
# these three names may ever be echoed into an error message.
_HINT_KEYS = ("team_id", "channel_id", "chat_id")
_ECHOABLE_FIELDS = frozenset(_HINT_KEYS)


class PreProcessNode(FunctionNode):
    """Validate + serialize caller input for the inner workflow graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Graph API call runs under this same
    # (unelevated) context, so the external gate lives HERE, not on the inner
    # API node. An under-trusted (ANONYMOUS) caller is denied at this gate
    # before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only [C1]

        if not user_input or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # Caller context must be a mapping; anything else is refused without
        # being echoed (fail closed - never guess at a malformed contract).
        if input_context is None:
            input_context = {}
        if not isinstance(input_context, dict):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: input_context must be an object"],
            }

        # ── Instruction-override screen (template-owned, fail CLOSED) ─────────
        # HTML stripping is not refusal: text that re-instructs the MODEL must
        # ERROR the request, with nothing carried forward - no validated_input,
        # no target_hint. This refusal lives in execute(), so it holds even
        # with no framework input gate in front (the 1.0.1 gate is
        # defence-in-depth here, never the guarantee - see security.py).
        #
        # Both sides of the sanitize are screened, because stripping mutates
        # the match surface in BOTH directions: it deletes chat-template
        # tokens (<|im_start|> is "<...>"-shaped - only the RAW text still
        # shows it), and it fuses tag-split words ("ig<b>nore</b> all previous
        # instructions" - only the STRIPPED text shows the phrase). The
        # stripped text is what would be carried forward.
        raw_text = user_input.strip()
        sanitized_input = sanitize_query(raw_text)
        if INSTRUCTION_OVERRIDE_RE.search(raw_text) or INSTRUCTION_OVERRIDE_RE.search(sanitized_input):
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input refused - instruction-override directive"],
            }

        # The structured channel gets the same screen: every string in
        # input_context (keys AND values, any depth) is walked, so a directive
        # riding in an ignored key - or AS a key - still fails the request. The
        # returned path masks caller-controlled names (only the three known
        # hint fields may be echoed); the hostile string itself never is.
        flagged_path = find_instruction_override(input_context, known_fields=_ECHOABLE_FIELDS, root="input_context")
        if flagged_path is not None:
            emit_trace_event(
                "pre_process_validation_failed",
                {"reason": "instruction_override"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: {flagged_path} refused - instruction-override directive"],
            }

        # Target team/channel/chat is caller-supplied and never inferred here:
        # validate any explicit id from input_context against the bounded
        # Teams-id shape. A present-but-malformed value (wrong type, wrong
        # charset, over-length) is a hard error naming the FIELD, never the
        # value - silently dropping it could post to a different target than
        # the caller intended. Local dict per invocation - never a module-global.
        target_hint: dict[str, str] = {}
        for key in _HINT_KEYS:
            value = input_context.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: input_context.{key} must be a string Teams id"],
                }
            value = value.strip()
            if not value:
                continue  # blank = absent
            if not TEAMS_ID_RE.match(value):
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: input_context.{key} is not a valid Teams id"],
                }
            target_hint[key] = value

        validated_input = json.dumps({"text": sanitized_input, "target_hint": target_hint})

        # Audit the shaped request - hint presence only, not the raw text.
        emit_trace_event(
            "pre_process_complete",
            {"has_target_hint": bool(target_hint)},
            state,
        )

        return {
            "validated_input": validated_input,
            "target_hint": to_json(target_hint) if target_hint else None,
            "status": AgentStatus.SUCCESS.value,
        }
