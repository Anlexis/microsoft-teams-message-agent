# CMN-C2-234 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: post_channel_message / post_chat_message (deterministic keyword
# heuristic, v1 - no LLM; chat/DM signals win, unknown falls back to
# post_channel_message with a non-fatal note - write safety is enforced
# downstream by the explicit-target-only rule).
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline around execute()); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_channel_message(self):
        result = self.node(_state('Post a channel message saying "build is green"'))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "post_channel_message"

    def test_keyword_chat_message(self):
        result = self.node(_state('Send a chat message saying "ping"'))
        assert result["intent"] == "post_chat_message"

    def test_chat_signal_wins_over_channel_signal(self):
        # Priority order is chat-first: "post a direct message about the team
        # offsite" carries both signals and must classify as the chat post.
        result = self.node(_state("Post a direct message about the team offsite"))
        assert result["intent"] == "post_chat_message"

    def test_dm_keyword_classifies_as_chat(self):
        result = self.node(_state('send a dm to the project group saying "standup moved"'))
        assert result["intent"] == "post_chat_message"

    def test_japanese_chat_keyword(self):
        result = self.node(_state("チャットで一言送ってください"))
        assert result["intent"] == "post_chat_message"

    def test_no_signal_defaults_to_channel_with_note(self):
        result = self.node(_state("please handle this request for me"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "post_channel_message"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to post_channel_message" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state('Post a channel message saying "build is green"'))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "post_channel_message"
        assert payloads["classify_intent_complete"]["defaulted"] is False
