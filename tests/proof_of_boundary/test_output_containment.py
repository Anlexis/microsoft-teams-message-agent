# PB-9: Blocked-response containment (CMN-C2-234).
#
# When the post_process output gate blocks a response, the caller must receive
# NOTHING from the blocked run. An error status alone does not achieve that:
# the base envelope resolves the caller-facing output as
# `formatted_output or result`, with no status check, so an error that leaves
# those fields standing still ships the un-gated inner answer to the caller
# inside the error envelope - and a node that raises instead returns a bare
# error partial that clears nothing and carries a traceback.
#
# These tests assert the ENVELOPE the caller receives. It is built by the
# agent's own get_output() - the same call the graph runtime hands back from
# invoke() - over a REAL run of the outer backbone: the real pre_process node,
# then the real main slot, which runs the real inner Teams workflow graph on
# the network-free stub transport, then the real post_process node through the
# full node pipeline.
#
# A gate that refused everything would satisfy the containment assertions on
# their own, so the containment cases are paired with a clean-path control:
# the same real run, un-blocked, must deliver the real confirmation and the
# real message evidence to the caller.

import json
import pathlib

import pytest

try:
    import yaml

    from framework.graph.base_graph import CURRENT_SCHEMA_VERSION
    from framework.schemas.agent_status import AgentStatus
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    from src.graph.graph import TeamsMessageAgent, TeamsWorkflowGraphNode
    from src.nodes.post_process_node import (
        _ERROR_ENVELOPE_KEYS,
        _REASON_OUTPUT_WITHHELD,
        _REASON_WORKFLOW_FAILED,
        PostProcessNode,
    )
    from src.nodes.pre_process_node import PreProcessNode
    from src.schemas.state import to_json

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when the framework wheel is absent
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")

_REQUEST = (
    "Post a channel message in team id a1b2c3d4-e5f6-4711-8abc-9def01234567 "
    'channel id 19:abcdef1234567890@thread.tacv2 saying "release 1.4 is live"'
)
# The caller's own words, carried through the whole pipeline. If this string
# reaches a blocked caller, the response was not contained.
_RELEASED_TEXT = "release 1.4 is live"

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


@pytest.fixture(scope="module")
def agent():
    config = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    graph = TeamsMessageAgent(config=config)
    graph.compile()
    return graph


@pytest.fixture
def post_main_state():
    """The real outer state as it stands when post_process is about to run."""
    ctx = InvocationContext(session_id="containment-001", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    state = {
        "user_input": _REQUEST,
        "input_context": {},
        "session_id": ctx.session_id,
        "correlation_id": ctx.correlation_id,
        "trace_id": "containment-trace",
        "thread_id": ctx.thread_id,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "message_id": "",
        "request_source": "",
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "caller_id": "",
        "hitl_allowed": False,
        "status": AgentStatus.PENDING.value,
        "retry_count": 0,
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(PreProcessNode()(state))
    state.update(TeamsWorkflowGraphNode()(state))
    # The run really did produce an answer - otherwise there is nothing to contain.
    assert state["status"] == AgentStatus.SUCCESS.value, state.get("error_log")
    assert _RELEASED_TEXT in json.dumps(state["result"], default=str)
    return state


def _caller_envelope(agent, state):
    """Run post_process through the full node pipeline, apply its partial the
    way the graph runtime does, and build the envelope the caller receives."""
    partial = PostProcessNode()(dict(state))
    merged = dict(state)
    merged.update(partial)
    return partial, agent.get_output(merged)


def _assert_contained(partial, envelope, evidence):
    """The blocked-response invariant, asserted on the caller's envelope."""
    blob = json.dumps(envelope, default=str)

    assert envelope["status"] == AgentStatus.ERROR.value
    # Truthy on purpose: a falsy output re-activates the `or result` fallback,
    # which hands the caller the un-gated inner answer instead of the notice.
    assert envelope["output"], "blocked output must stay truthy"
    assert envelope["output"].get("withheld") is True
    # Closed set: the caller's output is exactly the declared envelope - no
    # violation text, no error_log line, no id, nothing derived from either.
    assert envelope["output"] == {"withheld": True, "reason": _REASON_OUTPUT_WITHHELD}
    assert set(envelope["output"]) == set(_ERROR_ENVELOPE_KEYS)

    assert _RELEASED_TEXT not in blob, "released text reached the blocked caller"
    assert "Traceback" not in blob, "traceback reached the blocked caller"
    assert ".py" not in blob, "source path reached the blocked caller"
    for reference in evidence:
        if reference:
            assert reference not in blob, "message reference reached the blocked caller"

    # The state the gate leaves behind carries no output either.
    assert partial["result"] is None
    assert partial["formatted_output"]["withheld"] is True
    assert partial["message_preview"] == ""
    assert partial["confirmation"] == ""
    assert partial["teams_payload"] is None
    assert partial["message_id"] == ""
    assert partial["message_ref"] == ""


class TestCleanPathControl:
    """Without this, a gate that blocked every response would pass the suite."""

    def test_clean_run_delivers_the_real_answer(self, agent, post_main_state):
        partial, envelope = _caller_envelope(agent, post_main_state)
        assert partial["status"] == AgentStatus.SUCCESS.value
        assert envelope["status"] == AgentStatus.SUCCESS.value
        assert _RELEASED_TEXT in json.dumps(envelope, default=str)
        assert envelope["output"]["message_id"] or envelope["output"]["message_ref"]
        assert envelope["output"]["confirmation"]


class TestBlockedResponseContainment:
    def test_missing_message_evidence_is_contained(self, agent, post_main_state):
        """The gate's own block: a success-shaped output with no evidence that
        the message was posted. The answer the run produced must not ride out."""
        state = dict(post_main_state)
        evidence = (state.get("message_id"), state.get("message_ref"))
        state["message_id"] = ""
        state["message_ref"] = ""
        partial, envelope = _caller_envelope(agent, state)
        assert any("message_id/message_ref" in entry for entry in partial["error_log"])
        _assert_contained(partial, envelope, evidence)

    def test_credential_nested_in_the_payload_is_contained(self, agent, post_main_state):
        """A credential-shaped string riding inside the assembled chatMessage
        payload blocks the response - and nothing from that response ships."""
        bearer_like = "Bearer " + "a" * 24
        state = dict(post_main_state)
        evidence = (state.get("message_id"), state.get("message_ref"))
        state["teams_payload"] = to_json({"body": {"contentType": "text", "content": bearer_like}})
        partial, envelope = _caller_envelope(agent, state)
        assert any("teams_payload.body.content" in entry for entry in partial["error_log"])
        # The violation names the location, never the value.
        assert bearer_like not in "\n".join(partial["error_log"])
        assert bearer_like not in json.dumps(envelope, default=str)
        _assert_contained(partial, envelope, evidence)

    def test_credential_class_only_the_framework_scan_knows_is_contained(self, agent, post_main_state):
        """Containment has to be REACHABLE. A credential class the framework's
        own output scan catches but this node's gate misses is raised by the
        node wrapper instead, and the raised path returns a bare error partial
        that clears nothing and carries a traceback - so the gate must share
        the framework's recognizer. Asserted by outcome: blocked, contained,
        and no traceback (a raise would have produced one)."""
        aws_like = "AKIA" + "S" * 16
        state = dict(post_main_state)
        evidence = (state.get("message_id"), state.get("message_ref"))
        state["teams_payload"] = to_json({"body": {"contentType": "text", "content": aws_like}})
        partial, envelope = _caller_envelope(agent, state)
        assert all("Traceback" not in entry for entry in partial["error_log"])
        assert aws_like not in "\n".join(partial["error_log"])
        assert aws_like not in json.dumps(envelope, default=str)
        _assert_contained(partial, envelope, evidence)


# Node-authored text that must never reach the caller: a person's name, an
# identifier and a token-shaped value of the kind an upstream API error body
# carries. Assembled at runtime so no credential-shaped literal is committed.
_SENTINEL = "boom: upstream said {'customer':'A. Tanaka','token':'" + "sk-live-" + "x" * 3 + "'}"
_SENTINEL_FRAGMENTS = ("boom", "upstream said", "A. Tanaka", "sk-live")


class TestErrorEnvelopeAtTheCaller:
    """The ERROR envelope the caller receives is the closed set on EVERY
    non-success path, and an error_log line reaches nowhere in it."""

    def test_inner_workflow_error_reaches_the_caller_as_closed_set_only(self, agent, post_main_state):
        """The inner workflow ended in ERROR with upstream text in error_log.
        Driven through execute() directly: the real pipeline short-circuits
        post_process on an errored state (BaseNode.__call__ skips execute()),
        so execute() itself is the surface on this path. The base envelope
        then resolves `formatted_output or result` over the merged state."""
        state = dict(post_main_state)
        evidence = (state.get("message_id"), state.get("message_ref"))
        state["status"] = AgentStatus.ERROR.value
        state["error_log"] = [_SENTINEL]
        partial = PostProcessNode().execute(dict(state))
        merged = dict(state)
        merged.update(partial)
        envelope = agent.get_output(merged)
        blob = json.dumps(envelope, default=str)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert envelope["output"] == {"withheld": True, "reason": _REASON_WORKFLOW_FAILED}
        for fragment in _SENTINEL_FRAGMENTS:
            assert fragment not in blob, f"{fragment!r} reached the caller"
        assert _RELEASED_TEXT not in blob, "released text reached the errored caller"
        for reference in evidence:
            if reference:
                assert reference not in blob, "message reference reached the errored caller"
        # Structured product keys stay withheld on a non-SUCCESS run.
        assert "message_id" not in envelope
        assert "message_ref" not in envelope

    @pytest.mark.parametrize("block", ["missing-evidence", "credential-in-payload"])
    def test_gate_block_reaches_the_caller_as_closed_set_only(self, agent, post_main_state, block):
        """Both gate blocks through the FULL node pipeline, with an error_log
        line already present from upstream: the caller's output is exactly the
        closed-set envelope, and neither the upstream line nor the gate's own
        violation text appears anywhere in the caller's envelope."""
        state = dict(post_main_state)
        state["error_log"] = [_SENTINEL]
        if block == "missing-evidence":
            state["message_id"] = ""
            state["message_ref"] = ""
        else:
            state["teams_payload"] = to_json({"body": {"contentType": "text", "content": "Bearer " + "h" * 24}})
        partial, envelope = _caller_envelope(agent, state)
        blob = json.dumps(envelope, default=str)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert envelope["output"] == {"withheld": True, "reason": _REASON_OUTPUT_WITHHELD}
        for fragment in _SENTINEL_FRAGMENTS:
            assert fragment not in blob, f"{fragment!r} reached the caller"
        assert partial["error_log"], "the gate must record its violation operator-side"
        for violation in partial["error_log"]:
            assert violation not in blob, "violation text reached the caller"
