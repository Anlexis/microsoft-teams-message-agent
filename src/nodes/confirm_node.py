"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the posted Teams message (id + reference + preview) into a
human-readable confirmation message, surfacing the affected channel/chat
message for human review (risk mitigation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VERBS = {
    "post_channel_message": "Posted channel message",
    "post_chat_message": "Posted chat message",
}


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # Inner domain node, read-only formatting of already-produced data -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        message_id = state.get("message_id", "")
        message_ref = state.get("message_ref", "")
        message_preview = state.get("message_preview", "")
        intent = state.get("intent", "post_channel_message")

        if not message_id and not message_ref:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no message_id/message_ref to confirm"],
            }

        verb = _VERBS.get(intent, "Posted Teams message")
        parts = [f"{verb} '{message_preview or message_id}'"]
        if message_ref:
            parts.append(f"ref={message_ref}")
        if message_id:
            parts.append(f"id={message_id}")
        confirmation = " - ".join(parts)

        # Audit the confirmed action - intent + reference presence (no content).
        emit_trace_event(
            "confirm_complete",
            {"intent": intent, "has_message_ref": bool(message_ref)},
            state,
        )

        return {
            "confirmation": confirmation,
            "result": {
                "message_id": message_id,
                "message_ref": message_ref,
                "confirmation": confirmation,
            },
            "status": AgentStatus.SUCCESS.value,
        }
