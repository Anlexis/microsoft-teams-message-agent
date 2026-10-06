# CMN-C2-234 - Unit tests: PostProcessNode (outer backbone, domain output gate)
#
# Canon: invoked via node(state) (BaseNode.__call__ routes the full security
# pipeline around execute()); this backbone formatter declares ANONYMOUS -> the
# state builder sets caller_trust_level = TrustLevel.ANONYMOUS.value. The
# domain output gate is the MODULE-LEVEL _security_gate_output() helper (the
# framework gate methods are @final and the real SDK auto-wraps _extra_ hooks),
# so the helper is also unit-tested directly as a plain function. The gate
# walks NESTED structures: the caller-facing output embeds the assembled
# chatMessage payload as a nested dict, so a credential-shaped string one
# level down must be caught exactly like a top-level one.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import (
    _ERROR_ENVELOPE_KEYS,
    _ERROR_REASONS,
    _REASON_OUTPUT_WITHHELD,
    _REASON_WORKFLOW_FAILED,
    PostProcessNode,
    _security_gate_output,
)
from src.schemas.state import to_json

_MESSAGE_ID = "1738437"
_MESSAGE_REF = "https://teams.microsoft.com/l/message/19:abcdef1234567890@thread.tacv2/1738437"


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "message_id": _MESSAGE_ID,
        "message_ref": _MESSAGE_REF,
        "message_preview": "hello from the ops channel",
        "intent": "post_channel_message",
        "confirmation": f"Posted channel message 'hello from the ops channel' - ref={_MESSAGE_REF} - id={_MESSAGE_ID}",
        "teams_payload": to_json({"body": {"contentType": "text", "content": "hello from the ops channel"}}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        # Regression guard: AgentStatus is a str-Enum, so == passes for
        # the bare enum too - only an exact class check catches a bare-enum write.
        assert result["status"].__class__ is str
        out = result["formatted_output"]
        assert out["message_id"] == _MESSAGE_ID
        assert out["message_ref"] == _MESSAGE_REF
        assert out["intent"] == "post_channel_message"
        assert out["confirmation"].startswith("Posted channel message")
        # State-contract round-trip: the JSON teams_payload string surfaces parsed.
        assert out["teams_payload"] == {"body": {"contentType": "text", "content": "hello from the ops channel"}}

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            message_id="",
            message_ref="",
            error_log=["CallTeamsApiNode: Microsoft Graph API error 403: forbidden by Graph permissions"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "Microsoft Graph API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_status_as_string_value_preserved(self):
        """The framework may carry status as the enum .value (string) at the boundary."""
        result = self.node(_state(status=AgentStatus.ERROR.value, error_log=["boom"]))
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_gate_blocks_success_without_message_evidence(self):
        """Full node path: a SUCCESS output missing message_id/message_ref is blocked."""
        result = self.node(_state(message_id="", message_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("message_id/message_ref" in entry for entry in result["error_log"])


class TestSecurityGateOutputHelper:
    """The module-level domain output gate as a plain function (not a node call)."""

    def test_passes_success_with_message_evidence(self):
        violations = _security_gate_output(
            {"message_id": _MESSAGE_ID, "message_ref": _MESSAGE_REF, "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []

    def test_blocks_success_without_message_evidence(self):
        violations = _security_gate_output(
            {"message_id": "", "message_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "message_id/message_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"message_id": _MESSAGE_ID, "note": bearer_like},
            is_success=True,
        )
        assert len(violations) == 1
        # "note" is not one of the known output field names, so the location is
        # masked - a violation says WHERE, never WHAT, and never repeats a
        # caller-controlled field name either.
        assert "<unrecognised-field>" in violations[0]
        assert bearer_like not in violations[0]

    def test_known_field_name_is_named_in_the_violation(self):
        """Control for the masking rule: a KNOWN output field IS named, so the
        masking is a targeted rule and not a blanket that hides every location."""
        bearer_like = "Bearer " + "f" * 24
        violations = _security_gate_output(
            {"message_id": _MESSAGE_ID, "message_preview": bearer_like},
            is_success=True,
        )
        assert len(violations) == 1
        assert "message_preview" in violations[0]
        assert bearer_like not in violations[0]

    def test_blocks_credential_classes_the_framework_scan_would_raise_on(self):
        """The domain gate must be at least as wide as the framework's own
        output scan. A class the framework catches but this gate misses is
        raised by the node wrapper instead - and the raised path returns a bare
        error partial that clears nothing, so containment never happens."""
        for credential_like in (
            "AKIA" + "S" * 16,  # AWS access key id
            "sk_live_" + "b" * 20,  # Stripe secret key
            "postgresql://user:" + "c" * 12 + "@db.internal/app",  # connection string
        ):
            violations = _security_gate_output(
                {"message_id": _MESSAGE_ID, "message_preview": credential_like},
                is_success=True,
            )
            assert violations, f"gate missed {credential_like[:8]}..."
            assert credential_like not in "\n".join(violations)

    def test_error_output_not_required_to_carry_evidence(self):
        violations = _security_gate_output({"message_id": "", "message_ref": ""}, is_success=False)
        assert violations == []


class TestGateWalksNestedStructures:
    """The stated invariant is "no credential-shaped string reaches the caller"
    - for EVERY representation. The caller-facing output embeds the assembled
    chatMessage payload as a NESTED dict, so the gate must walk nested
    mappings/lists, not only top-level strings. The top-level case doubles as
    the control that proves the scanner itself works."""

    def test_top_level_control_is_caught(self):
        # Control: proves the scanner works where the old gate already looked.
        bearer_like = "Bearer " + "a" * 24
        violations = _security_gate_output(
            {"message_id": _MESSAGE_ID, "note": bearer_like},
            is_success=True,
        )
        assert len(violations) == 1

    def test_nested_credential_in_payload_body_is_caught(self):
        # The leak case: a token riding INSIDE the nested chatMessage payload.
        bearer_like = "Bearer " + "b" * 24
        violations = _security_gate_output(
            {
                "message_id": _MESSAGE_ID,
                "message_ref": _MESSAGE_REF,
                "teams_payload": {"body": {"contentType": "text", "content": f"deploy with {bearer_like}"}},
            },
            is_success=True,
        )
        assert len(violations) == 1
        # The violation names the field PATH, never the value.
        assert "teams_payload.body.content" in violations[0]
        assert bearer_like not in violations[0]

    def test_credential_inside_list_is_caught(self):
        jwt_like = "eyJ" + "c" * 40
        violations = _security_gate_output(
            {"message_id": _MESSAGE_ID, "notes": ["fine", f"token {jwt_like}"]},
            is_success=True,
        )
        assert len(violations) == 1
        assert "<unrecognised-field>[1]" in violations[0]
        assert jwt_like not in violations[0]

    def test_full_node_path_blocks_nested_credential(self, monkeypatch):
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)
        bearer_like = "Bearer " + "d" * 24
        node = PostProcessNode()
        result = node(_state(teams_payload=to_json({"body": {"contentType": "text", "content": bearer_like}})))
        assert result["status"] == AgentStatus.ERROR.value
        assert bearer_like not in "\n".join(result["error_log"])
        # The blocked response carries the withheld notice, not the output.
        assert result["formatted_output"]["withheld"] is True
        assert bearer_like not in str(result["formatted_output"])

    def test_clean_nested_payload_passes(self):
        violations = _security_gate_output(
            {
                "message_id": _MESSAGE_ID,
                "message_ref": _MESSAGE_REF,
                "teams_payload": {"body": {"contentType": "text", "content": "hello from the ops channel"}},
            },
            is_success=True,
        )
        assert violations == []


def _strings_in(value):
    """Every string leaf in a nested mapping/sequence - keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings_in(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _strings_in(item)


# Node-authored text a caller must never see: a person's name, an identifier
# and a token-shaped value inside an upstream response - the kind of line an
# API error handler writes to error_log. Assembled at runtime so no
# credential-shaped literal is committed. Credential-only redaction would
# leave the name and the id standing, which is why redaction is not the fix.
_SENTINEL = "boom: upstream said {'customer':'A. Tanaka','token':'" + "sk-live-" + "x" * 3 + "'}"
_SENTINEL_FRAGMENTS = ("boom", "upstream said", "A. Tanaka", "sk-live")

_BEARER_LIKE = "Bearer " + "g" * 24


def _errored_state() -> dict:
    """Path 1: the inner workflow ended in ERROR; error_log carries its text."""
    return _state(status=AgentStatus.ERROR.value, message_id="", message_ref="", error_log=[_SENTINEL])


def _no_evidence_state() -> dict:
    """Path 2: gate violation - SUCCESS without message evidence."""
    return _state(message_id="", message_ref="", error_log=[_SENTINEL])


def _credential_state() -> dict:
    """Path 3: gate violation - credential-shaped value nested in the payload."""
    return _state(
        teams_payload=to_json({"body": {"contentType": "text", "content": _BEARER_LIKE}}),
        error_log=[_SENTINEL],
    )


_ERROR_PATHS = [
    pytest.param(_errored_state, _REASON_WORKFLOW_FAILED, id="inner-workflow-error"),
    pytest.param(_no_evidence_state, _REASON_OUTPUT_WITHHELD, id="gate-missing-evidence"),
    pytest.param(_credential_state, _REASON_OUTPUT_WITHHELD, id="gate-credential-in-payload"),
]
_GATE_PATHS = _ERROR_PATHS[1:]


class TestErrorEnvelopeClosedSet:
    """The caller-visible ERROR envelope carries closed-set labels only, on
    EVERY non-success path. error_log is the internal channel and stays as it
    is; nothing from it - and no id, name, violation text or upstream response
    body - is projected into formatted_output. Driven through execute()
    directly: on the errored path the real pipeline short-circuits before
    execute() (see test_error_status_preserved), so execute() IS the surface
    under test; the gate paths are additionally driven through the full node
    pipeline below."""

    @pytest.mark.parametrize("build_state, expected_reason", _ERROR_PATHS)
    def test_envelope_values_are_declared_constants(self, build_state, expected_reason):
        result = PostProcessNode().execute(build_state())
        assert result["status"] == AgentStatus.ERROR.value
        envelope = result["formatted_output"]
        assert set(envelope) == set(_ERROR_ENVELOPE_KEYS)
        assert envelope["withheld"] is True
        assert envelope["reason"] == expected_reason
        assert envelope["reason"] in _ERROR_REASONS
        # Every value is a declared constant - nothing computed from state.
        for value in envelope.values():
            assert value is True or value in _ERROR_REASONS

    @pytest.mark.parametrize("build_state, expected_reason", _ERROR_PATHS)
    def test_envelope_is_truthy(self, build_state, expected_reason):
        # A falsy formatted_output re-opens the base envelope's `or result` fallback.
        result = PostProcessNode().execute(build_state())
        assert result["formatted_output"], "ERROR envelope must stay truthy"

    @pytest.mark.parametrize("build_state, expected_reason", _ERROR_PATHS)
    def test_error_log_sentinel_reaches_nowhere_in_the_returned_mapping(self, build_state, expected_reason):
        result = PostProcessNode().execute(build_state())
        leaves = list(_strings_in(result))
        for fragment in _SENTINEL_FRAGMENTS:
            assert not any(fragment in leaf for leaf in leaves), f"{fragment!r} reached the returned mapping"
        assert _SENTINEL not in json.dumps(result, default=str)

    @pytest.mark.parametrize("build_state, expected_reason", _ERROR_PATHS)
    def test_output_bearing_fields_are_cleared(self, build_state, expected_reason):
        result = PostProcessNode().execute(build_state())
        assert result["result"] is None
        assert result["message_preview"] == ""
        assert result["confirmation"] == ""
        assert result["teams_payload"] is None
        assert result["message_id"] == ""
        assert result["message_ref"] == ""

    def test_errored_path_does_not_re_emit_error_log(self):
        # The state reducer appends; re-emitting the inner entries would
        # duplicate every line. Nothing new was authored here either.
        result = PostProcessNode().execute(_errored_state())
        assert "error_log" not in result

    @pytest.mark.parametrize("build_state, expected_reason", _GATE_PATHS)
    def test_gate_violations_go_to_error_log_not_the_envelope(self, build_state, expected_reason):
        result = PostProcessNode().execute(build_state())
        violations = result["error_log"]
        assert violations and all(entry.startswith("PostProcess output gate:") for entry in violations)
        # Violation text is operator-side only; the caller field never carries it.
        envelope_blob = json.dumps(result["formatted_output"], default=str)
        for violation in violations:
            assert violation not in envelope_blob
        assert _SENTINEL not in violations

    @pytest.mark.parametrize("build_state, expected_reason", _GATE_PATHS)
    def test_full_pipeline_gate_path_carries_closed_set_envelope(self, build_state, expected_reason):
        result = PostProcessNode()(build_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"] == {"withheld": True, "reason": expected_reason}
        for fragment in _SENTINEL_FRAGMENTS:
            assert not any(fragment in leaf for leaf in _strings_in(result["formatted_output"]))
        assert _BEARER_LIKE not in json.dumps(result["formatted_output"], default=str)
