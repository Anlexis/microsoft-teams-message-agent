# CMN-C2-234 - Unit tests: inner TeamsWorkflowGraph (BaseGraph) contract.
# The compiled outer path is
# exercised end-to-end by tests/proof_of_boundary/test_pb_invoke_order.py; this
# module unit-checks the inner graph's identity, config forwarding, routing,
# output contract, and a direct inner invoke on the network-free v1 stub.


from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import TeamsWorkflowGraph
from src.schemas.state import State, from_json

_TEAM_ID = "a1b2c3d4-e5f6-4711-8abc-9def01234567"
_CHANNEL_ID = "19:abcdef1234567890@thread.tacv2"


def _graph(config=None):
    return TeamsWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "teams_message_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_teams_config_as_json():
    g = _graph({"configurable": {"teams": {"base_url": "https://graph.example.test/v1.0"}}})
    extra = g._extra_initial_state()
    # State contract: forwarded as a JSON string, not a native dict.
    assert isinstance(extra["teams_config"], str)
    assert from_json(extra["teams_config"], {}) == {"base_url": "https://graph.example.test/v1.0"}


def test_extra_initial_state_empty_without_teams_section():
    assert _graph()._extra_initial_state() == {}


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_get_output_surfaces_message_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"message_id": "1738437", "message_ref": "teams://ref", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "post_channel_message",
            "team_id": _TEAM_ID,
            "channel_id": _CHANNEL_ID,
            "chat_id": "",
            "message_id": "1738437",
            "message_ref": "teams://ref",
            "message_preview": "hello",
            "confirmation": "ok",
            "teams_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "post_channel_message"
    assert out["message_ref"] == "teams://ref"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"message_id": "1738437", "message_ref": "teams://ref", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_channel_post_on_v1_stub():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"teams": {"base_url": "https://graph.microsoft.com/v1.0"}}})
    g.compile()
    result = g.invoke(
        user_input=(
            f"Post a channel message in team id {_TEAM_ID} channel id {_CHANNEL_ID} "
            'saying "hello from the inner graph"'
        )
    )
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["intent"] == "post_channel_message"
    assert result["message_id"]
    assert result["message_ref"].startswith("https://teams.microsoft.com/l/message/")
    assert result["confirmation"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferTeamsFieldsNode",
        "CallTeamsApiNode",
        "ConfirmNode",
    ]
