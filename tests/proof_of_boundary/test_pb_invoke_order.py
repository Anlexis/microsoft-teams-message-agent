# PB-6: Backbone invoke-order + external-trust boundary (CMN-C2-234).
#
# A real outer-graph invoke exercising the fixed 5-node backbone
# (initialize -> pre_process -> main[inner Teams workflow] -> post_process -> finalize).
#
# The SINGLE external trust gate lives on the outer backbone pre_process
# (required_trust_level = VERIFIED_EXTERNAL). GraphNode.execute() passes the
# caller's InvocationContext into the inner subgraph UNCHANGED (no trust
# elevation), so the inner Graph API call (CallTeamsApiNode) is ANONYMOUS and
# runs under the caller's already-gated context. Two trust levels are asserted:
#
#   * VERIFIED_EXTERNAL (a real external caller - never for_internal()): passes
#     the pre_process gate, so the full backbone runs IN ORDER and the message
#     evidence surfaces in result["output"] (status success). The payload is
#     byte-equal to deploy/invoke_payload.json's "input" - this is exactly the
#     deployment first-invoke path.
#   * ANONYMOUS (an under-trusted caller): denied at the pre_process gate before
#     the inner Teams call can run -> status=error, no message evidence.
#
# The Teams call is served by the deterministic NETWORK-FREE v1 stub transport
# (no live tenant, no secret needed - the node runs on the documented stub
# placeholder). Message evidence is asserted by presence (mask-robust: the
# framework/FinalizeNode may mask raw identifiers), never by whole-repr.

import json
import pathlib

import pytest

try:
    from framework.schemas.agent_status import AgentStatus
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    from src.graph.graph import TeamsMessageAgent

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when the framework wheel is absent
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")

_MAIN_SLOT_NODE = "TeamsWorkflowGraphNode"

# Byte-equal to deploy/invoke_payload.json "input" (asserted below - never retyped drift).
_VALID_PAYLOAD = (
    "Post a channel message in team id a1b2c3d4-e5f6-4711-8abc-9def01234567 "
    "channel id 19:abcdef1234567890@thread.tacv2 "
    'saying "Deployment check - hello from the Teams message agent."'
)
_SESSION_ID = "deploy-check-001"

_PAYLOAD_PATH = pathlib.Path(__file__).parents[2] / "deploy" / "invoke_payload.json"

_EXPECTED_HISTORY = [
    "InitializeNode",
    "PreProcessNode",
    _MAIN_SLOT_NODE,
    "PostProcessNode",
    "FinalizeNode",
]


def _build_agent():
    agent = TeamsMessageAgent()
    agent.compile()
    return agent


class TestBackboneInvokeOrder:
    def test_valid_payload_is_byte_equal_to_deploy_payload(self):
        """PB-6 uses the deployment first-invoke request verbatim."""
        deployed = json.loads(_PAYLOAD_PATH.read_text(encoding="utf-8"))
        assert deployed["input"] == _VALID_PAYLOAD
        assert deployed["session_id"] == _SESSION_ID

    def test_verified_external_runs_full_backbone_in_order(self):
        """External (VERIFIED_EXTERNAL) caller: the exact 5-node backbone runs
        in order and message evidence surfaces in result["output"]."""
        agent = _build_agent()
        result = agent.invoke(
            user_input=_VALID_PAYLOAD,
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        assert result.get("trace_id") and result.get("correlation_id")
        assert result.get("node_history", []) == _EXPECTED_HISTORY
        # Message evidence + confirmation surface in result["output"] (presence,
        # mask-robust - never the raw identifier value).
        out = result.get("output") or {}
        assert out.get("message_id") or out.get("message_ref")
        assert out.get("confirmation")
        assert out.get("intent") == "post_channel_message"

    def test_under_trusted_caller_is_denied(self):
        """Under-trusted (ANONYMOUS) caller: ANONYMOUS < VERIFIED_EXTERNAL, so
        the pre_process gate refuses the request before the inner Teams call
        can run - a well-formed error surface (gated, not crashed) with no
        message evidence. Note: the main slot NAME still appears in node_history
        because BaseNode.__call__ short-circuits on the already-errored state
        (its subgraph never executes); post_process is skipped by route()."""
        agent = _build_agent()
        result = agent.invoke(
            user_input=_VALID_PAYLOAD,
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-anon", caller_trust_level=TrustLevel.ANONYMOUS),
        )
        assert result["status"] == AgentStatus.ERROR.value, f"result={result!r}"
        assert "PostProcessNode" not in result.get("node_history", [])
        assert not result.get("output")

    def test_empty_input_surfaces_error_not_crash(self):
        agent = _build_agent()
        result = agent.invoke(
            user_input="   ",
            session_id=_SESSION_ID,
            ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )
        assert result["status"] == AgentStatus.ERROR.value
