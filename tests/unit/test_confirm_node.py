# CMN-C2-234 - Unit tests: ConfirmNode (inner Step 5)
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline around execute()); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode

_MESSAGE_ID = "1738437"
_MESSAGE_REF = "https://teams.microsoft.com/l/message/19:abcdef1234567890@thread.tacv2/1738437"


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "message_id": _MESSAGE_ID,
        "message_ref": _MESSAGE_REF,
        "message_preview": "hello from the ops channel",
        "intent": "post_channel_message",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_channel_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Posted channel message" in result["confirmation"]
        assert "'hello from the ops channel'" in result["confirmation"]
        assert f"ref={_MESSAGE_REF}" in result["confirmation"]
        assert f"id={_MESSAGE_ID}" in result["confirmation"]
        assert result["result"]["message_id"] == _MESSAGE_ID
        assert result["result"]["message_ref"] == _MESSAGE_REF

    def test_chat_verb(self):
        result = self.node(_state(intent="post_chat_message"))
        assert "Posted chat message" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Posted Teams message" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(message_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert f"id={_MESSAGE_ID}" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_message_id_when_preview_missing(self):
        result = self.node(_state(message_preview=""))
        assert f"'{_MESSAGE_ID}'" in result["confirmation"]

    def test_missing_message_evidence_errors(self):
        result = self.node(_state(message_id="", message_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
