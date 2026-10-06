"""AgentCore Platform v1.0 - inner Teams workflow graph (Cat 2 domain workflow).

Instantiated by TeamsWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_teams_fields
          -> call_teams_api -> confirm -> END

Config (forwarded from the outer graph via _parent_config(), under
config["configurable"]):
    teams    - config/config.yaml integration section (base_url, ...);
               injected into State as the JSON `teams_config` field via
               _extra_initial_state() so the no-arg nodes can read it
    llm      - reserved for the documented LLM-synthesis follow-up (docs/02
               "v1 Implementation Note"); unused in deterministic v1

Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_teams_fields_node import InferTeamsFieldsNode
from src.nodes.call_teams_api_node import CallTeamsApiNode
from src.nodes.confirm_node import ConfirmNode
from src.graph.context_bridge import get_caller_target_hint
from src.schemas.state import State, to_json


class TeamsWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "teams_message_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: the teams section is optional (the client
        # falls back to the documented default base_url + the network-free v1
        # stub transport), and a missing/unusable setting is handled at
        # CallTeamsApiNode.execute() as a graceful status=error rather than
        # a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_teams_fields"] = InferTeamsFieldsNode()
        self._nodes["call_teams_api"] = CallTeamsApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_teams_fields")
        self._sg.add_edge("infer_teams_fields", "call_teams_api")
        self._sg.add_edge("call_teams_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: AgentState) -> str:
        # Required by BaseGraph ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _extra_initial_state(self) -> dict[str, Any]:
        # Forward the config/config.yaml `teams` section (arriving under
        # config["configurable"] from _parent_config()) into State as a JSON
        # string (msgpack-safe) so the no-arg CallTeamsApiNode can
        # read it via state.get("teams_config"), and seed the caller's
        # VALIDATED target hint bridged by the outer graph (context_bridge.py
        # - the framework masks validated_input, so the hint cannot ride the
        # serialized request alone).
        extra: dict[str, Any] = {}
        configurable = self.config.get("configurable") or {}
        teams = configurable.get("teams") or {}
        if teams:
            extra["teams_config"] = to_json(teams)
        hint = get_caller_target_hint()
        if hint:
            extra["target_hint"] = to_json(hint)
        return extra

    def get_output(self, state: AgentState) -> dict[str, Any]:
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "team_id": state.get("team_id", ""),
            "channel_id": state.get("channel_id", ""),
            "chat_id": state.get("chat_id", ""),
            "message_id": state.get("message_id", ""),
            "message_ref": state.get("message_ref", ""),
            "message_preview": state.get("message_preview", ""),
            "confirmation": state.get("confirmation", ""),
            "teams_payload": state.get("teams_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
