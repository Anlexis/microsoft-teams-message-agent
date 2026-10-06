"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of post_channel_message /
post_chat_message using a deterministic keyword heuristic, so the template is
testable and runnable without a live LLM (v1 Implementation Note - LLM
synthesis, docs/02_design.md). Low-confidence / unknown falls back to
"post_channel_message" with a note - both intents are writes, so the write-
safety control is the explicit-target-only rule in InferTeamsFields /
CallTeamsApi (an unresolved target is a status=error; nothing is ever posted
to an un-named target).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("post_channel_message", "post_chat_message")

# Deterministic keyword signals (checked in priority order, the more specific
# chat/DM signals first so "send a direct message about the team offsite"
# classifies as the chat post, not the channel post).
_KEYWORDS = (
    (
        "post_chat_message",
        (
            "chat",
            "direct message",
            "private message",
            " dm ",
            "1:1",
            "1on1",
            "one-on-one",
            "group conversation",
            "チャット",
            "個別",
            "ダイレクト",
        ),
    ),
    (
        "post_channel_message",
        (
            "channel",
            "team",
            "post",
            "announce",
            "announcement",
            "broadcast",
            "notify",
            "share",
            "publish",
            "チャネル",
            "チーム",
            "投稿",
            "周知",
            "アナウンス",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a Teams message-posting intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = [
                "ClassifyIntentNode: low-confidence classification, "
                "defaulted to post_channel_message (explicit-target-only "
                "guard still applies - nothing posts without a named target)"
            ]
            intent = "post_channel_message"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note)},
            state,
        )

        result = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the channel default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
