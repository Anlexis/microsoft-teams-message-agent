# CMN-C2-234 - Unit tests: InferTeamsFieldsNode (inner Step 3)
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline around execute()); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.
# Positive payloads are PII-free. Teams channel-style ids
# (19:...@thread.tacv2) do not match the framework email-mask pattern; the
# real-style chat id (19:...@unq.gbl.spaces) WOULD match, so the chat-target
# test routes it through target_hint (a JSON state field the mask never
# scans) - exactly how a caller-supplied hint arrives in production.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_teams_fields_node import InferTeamsFieldsNode
from src.schemas.state import from_json, to_json

_TEAM_ID = "a1b2c3d4-e5f6-4711-8abc-9def01234567"
_CHANNEL_ID = "19:abcdef1234567890@thread.tacv2"


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_teams_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "post_channel_message", target_hint=None, **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "target_hint": to_json(target_hint) if target_hint else None,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferTeamsFieldsNode:
    def setup_method(self):
        self.node = InferTeamsFieldsNode()

    def test_channel_target_and_quoted_message_from_text(self):
        text = (
            f"Post a channel message in team id {_TEAM_ID} channel id {_CHANNEL_ID} "
            'saying "Deployment check - hello from the Teams message agent."'
        )
        result = self.node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["team_id"] == _TEAM_ID
        assert result["channel_id"] == _CHANNEL_ID
        assert result["message_preview"] == "Deployment check - hello from the Teams message agent."
        # State contract: teams_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["teams_payload"], str)
        assert from_json(result["teams_payload"], {}) == {
            "body": {"contentType": "text", "content": "Deployment check - hello from the Teams message agent."}
        }

    def test_chat_id_resolved_from_target_hint(self):
        # Real-style chat id travels via target_hint (never mask-scanned).
        result = self.node(
            _state(
                'Send a chat message saying "ping"',
                intent="post_chat_message",
                target_hint={"chat_id": "19:aa11bb22cc33@unq.gbl.spaces"},
            )
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["chat_id"] == "19:aa11bb22cc33@unq.gbl.spaces"
        assert from_json(result["teams_payload"], {})["body"]["content"] == "ping"

    def test_kv_lines_resolve_target_and_message(self):
        text = (
            "Post to the ops channel\n"
            f"Team: {_TEAM_ID}\n"
            f"Channel: {_CHANNEL_ID}\n"
            "Message: maintenance window tonight"
        )
        result = self.node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["team_id"] == _TEAM_ID
        assert result["channel_id"] == _CHANNEL_ID
        assert from_json(result["teams_payload"], {})["body"]["content"] == "maintenance window tonight"

    def test_unresolved_target_left_empty_never_invented(self):
        result = self.node(_state('Post a channel message saying "hello"'))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["team_id"] == ""
        assert result["channel_id"] == ""
        assert result["chat_id"] == ""

    def test_non_id_shaped_hint_left_unresolved(self):
        result = self.node(
            _state(
                'Post a channel message saying "hello"',
                target_hint={"team_id": "not a valid id!"},
            )
        )
        assert result["team_id"] == ""

    def test_unresolved_message_left_empty_never_the_whole_text(self):
        result = self.node(_state(f"Post a channel message in channel id {_CHANNEL_ID}"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["message_preview"] == ""
        assert from_json(result["teams_payload"], {})["body"]["content"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_presence_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.infer_teams_fields_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        text = f'Post a channel message in team id {_TEAM_ID} channel id {_CHANNEL_ID} saying "hello"'
        self.node(_state(text))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["infer_teams_fields_complete"]
        assert payload["intent"] == "post_channel_message"
        assert payload["has_team_id"] is True
        assert payload["has_channel_id"] is True
        assert payload["has_chat_id"] is False
        assert payload["has_message"] is True
        assert "content" not in payload
