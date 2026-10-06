# CMN-C2-234 - Unit tests: CallTeamsApiNode (inner Step 4, tool side-effect)
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline around execute()); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.
# The ONE documented exception: the config-override call passes a 2nd (config)
# argument, which __call__ cannot forward - that single test stays a DIRECT
# execute(state, config=...) call (ANONYMOUS node, trust gate unaffected).
#
# The node builds its client locally (SDK v1 nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's TeamsClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_teams_api_node import CallTeamsApiNode
from src.services.teams_client import TeamsApiError
from src.schemas.state import to_json

_TEAM_ID = "a1b2c3d4-e5f6-4711-8abc-9def01234567"
_CHANNEL_ID = "19:abcdef1234567890@thread.tacv2"
_CHAT_ID = "19:aa11bb22cc33@unq.gbl.spaces"
_MESSAGE = "hello from the ops channel"


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_teams_api_node.emit_trace_event", lambda *a, **k: None)


def _payload(content: str = _MESSAGE) -> str:
    return to_json({"body": {"contentType": "text", "content": content}})


def _state(**overrides) -> dict:
    state = {
        "teams_payload": _payload(),
        "intent": "post_channel_message",
        "team_id": _TEAM_ID,
        "channel_id": _CHANNEL_ID,
        "chat_id": "",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-teams-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for TeamsClient: the post raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def post_channel_message(self, team_id, channel_id, payload, api_token):
        raise TeamsApiError(403, "forbidden by Graph permissions")


class _FakeLiveClient:
    """Stands in for TeamsClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def post_channel_message(self, team_id, channel_id, payload, api_token):
        _FakeLiveClient.captured = {
            "team_id": team_id,
            "channel_id": channel_id,
            "payload": payload,
            "api_token": api_token,
        }
        return {"id": "1738437001", "webUrl": f"https://teams.microsoft.com/l/message/{channel_id}/1738437001"}


class TestCallTeamsApiNode:
    def setup_method(self):
        self.node = CallTeamsApiNode()

    def test_channel_post_success_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub; no secret
        # provider bound -> the node runs on the documented stub placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["message_id"]
        assert result["message_ref"].startswith("https://teams.microsoft.com/l/message/")
        assert _CHANNEL_ID in result["message_ref"]
        assert result["message_preview"] == _MESSAGE

    def test_chat_post_success_via_default_v1_stub(self):
        state = _state(intent="post_chat_message", team_id="", channel_id="", chat_id=_CHAT_ID)
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["message_id"]
        assert _CHAT_ID in result["message_ref"]

    def test_teams_config_state_field_sets_base_url(self):
        # The inner graph injects the manifest `teams:` section as the JSON
        # teams_config state field; the stub transport still serves the call.
        state = _state(teams_config=to_json({"base_url": "https://graph.example.test/v1.0"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["message_ref"]

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"teams": {"base_url": "https://graph.example.test/v1.0"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["message_id"]

    def test_missing_payload_errors(self):
        result = self.node(_state(teams_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_empty_message_body_refuses_to_post(self):
        result = self.node(_state(teams_payload=_payload("   ")))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("refusing to post an empty message" in entry for entry in result["error_log"])

    def test_unresolved_channel_target_errors(self):
        result = self.node(_state(team_id="", channel_id=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved team_id/channel_id" in entry for entry in result["error_log"])

    def test_unresolved_chat_target_errors(self):
        state = _state(intent="post_chat_message", chat_id="")
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved chat_id" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        result = self.node(_state(intent="delete_message"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_teams_api_node.TeamsClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # Secret posture: with a LIVE transport a missing TEAMS_GRAPH_TOKEN is a hard
        # error - a real API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_teams_api_node.TeamsClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_teams_api_node.TeamsClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"TEAMS_GRAPH_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["team_id"] == _TEAM_ID
        assert _FakeLiveClient.captured["channel_id"] == _CHANNEL_ID

    def test_audit_emits_side_effect_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_teams_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_teams_api_complete"]
        assert payload["intent"] == "post_channel_message"
        assert payload["has_message_id"] is True
        assert payload["stub_transport"] is True
