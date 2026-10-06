"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner Teams workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing `formatted_output`.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT the
framework `_extra_security_gate_output` hook - the framework gate methods
are @final on FunctionNode and the real SDK auto-wraps `_extra_` hooks (which
breaks the .invoke() chain), so domain checks live in a module-level helper
invoked inline. The gate walks the WHOLE nested output structure (dicts,
lists, tuples), not just top-level strings - the caller-facing output carries
the assembled chatMessage payload as a nested mapping, and a credential-shaped
string riding one level down must be caught exactly like a top-level one.

CONTAINMENT - a blocked response must release NOTHING:

  1. Returning ERROR is not containment. The base envelope resolves the
     caller-facing output as `formatted_output or result`, with no status
     check, so an ERROR that leaves those fields standing still hands the
     caller the un-gated inner answer inside the ERROR envelope. execute()
     therefore CLEARS every field carrying answer text, payload, or a
     reference to the posted message, and substitutes a withheld notice.
  2. The withheld notice must be TRUTHY. An empty string / empty mapping is
     falsy and re-activates the `or result` fallback, which is the same leak
     by another route.
  3. A violation names the LOCATION only, never the matched value. Quoting it
     would make the framework's own output scan raise on the returned delta,
     and a raised node returns a bare error partial that clears nothing -
     which puts the leak straight back and makes containment unreachable.
     Path components outside the known output field names are masked for the
     same reason.
  4. The domain scan uses the framework's own credential recognizer rather
     than a local approximation. A local pattern set narrower than the
     framework's lets a credential through this gate and into the framework
     scan, which raises - and the raised path clears nothing. Sharing the
     recognizer means anything the framework would raise on is blocked here
     first, where the clearing happens.
  5. The caller-visible ERROR envelope is a CLOSED SET. On EVERY non-success
     path (inner-workflow error, gate violation) `formatted_output` carries
     only values this module chose from its own constants: `withheld` and a
     `reason` code. Nothing is read from `error_log`, from the violations
     list, from a message id/ref, or from any other node-authored string.
     Those can embed identifiers, names and upstream API response bodies,
     and truncation, path stripping or credential-only redaction does not
     bound them. `error_log` stays as it is - it is the internal audit
     channel, the state reducer appends to it, and the base envelope never
     projects it to the caller.
"""

from typing import Any, Iterator

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Field names a violation message may echo. Every other path component is
# caller-influenced data and is masked: a violation says WHERE, never WHAT.
_ECHOABLE_OUTPUT_FIELDS = frozenset(
    {
        "message_id",
        "message_ref",
        "message_preview",
        "intent",
        "confirmation",
        "teams_payload",
        "body",
        "contentType",
        "content",
        "error",
    }
)
_MASKED_FIELD = "<unrecognised-field>"

# The closed set the caller-visible ERROR envelope is drawn from. A reason is
# a constant chosen here; it is never derived from error_log, a violation
# message, a state value or an exception instance.
_REASON_WORKFLOW_FAILED = "workflow_failed"  # the inner workflow ended in ERROR
_REASON_OUTPUT_WITHHELD = "output_withheld"  # the output gate blocked the response
_ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})
_ERROR_ENVELOPE_KEYS = frozenset({"withheld", "reason"})


def _error_envelope(reason: str) -> "dict[str, Any]":
    """The caller-visible ERROR envelope: closed-set labels only.

    TRUTHY on purpose (the constant keys guarantee it): the base envelope
    resolves the caller-facing output as `formatted_output or result`, so a
    falsy envelope would fall through to whatever survived in `result`. A
    fresh dict per call (never a shared mutable module constant). It carries
    no message, no id and no count - the audit trail (error_log + trace
    events) is where the detail lives, and it is not projected to the caller.
    """
    return {"withheld": True, "reason": reason}


def _contain(reason: str, new_errors: "list[str] | None" = None) -> "dict[str, Any]":
    """An ERROR result that releases nothing.

    One helper for EVERY non-success path, so the caller sees one contract:
    every output-bearing field cleared and the closed-set envelope as
    `formatted_output`. `new_errors` go to error_log - the internal channel -
    only when this node authored them (gate violations). On an already-errored
    state error_log already carries the inner entries and the state reducer
    appends, so re-emitting them here would duplicate every line.
    """
    contained: dict[str, Any] = {
        "status": AgentStatus.ERROR.value,
        "formatted_output": _error_envelope(reason),
        "result": None,
        "message_preview": "",
        "confirmation": "",
        "teams_payload": None,
        "message_id": "",
        "message_ref": "",
    }
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


def _iter_strings(value: Any, path: str) -> "Iterator[tuple[str, str]]":
    """Yield (masked path, string) for EVERY string leaf in a nested structure.

    Walks dicts, lists and tuples so a value nested inside the chatMessage
    payload (e.g. formatted_output["teams_payload"]["body"]["content"]) is
    scanned exactly like a top-level field. Keys outside the known output
    field names are masked, so the path is always safe to name in an error.
    """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            safe_key = str(key) if key in _ECHOABLE_OUTPUT_FIELDS else _MASKED_FIELD
            yield from _iter_strings(item, f"{path}.{safe_key}" if path else safe_key)
    elif isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            yield from _iter_strings(item, f"{path}[{idx}]")


def _security_gate_output(formatted_output: "dict[str, Any]", is_success: bool) -> "list[str]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Blocks (returns violations for):
      - a SUCCESS response with no message evidence (message_id/message_ref), which
        would misrepresent the Teams action outcome to the caller;
      - any credential-shaped string value ANYWHERE in the caller-facing output,
        including strings nested inside mappings/lists (the assembled
        chatMessage payload is a nested dict). Violations name the field PATH,
        never the value.
    """
    problems: list[str] = []
    if is_success and not (formatted_output.get("message_id") or formatted_output.get("message_ref")):
        problems.append("PostProcess output gate: SUCCESS output missing message_id/message_ref evidence")
    for path, value in _iter_strings(formatted_output, ""):
        if detect_credentials(value):
            problems.append(f"PostProcess output gate: credential-like value in formatted_output['{path}']")
    return problems


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # Read-only formatting of the already-produced result - default permissive;
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        errored = state.get("status") == AgentStatus.ERROR.value

        # An inner-workflow error is preserved (never masked as success) and
        # CONTAINED: the caller gets the closed-set envelope, nothing else.
        # error_log already holds the inner entries - upstream API error text
        # included - and none of it is projected: an identifier, a name or a
        # third-party response body is not bounded by truncation or
        # credential-only redaction. Under the real pipeline BaseNode.__call__
        # short-circuits on an errored state before execute() runs, so this
        # branch is defence in depth for direct invocation.
        if errored:
            emit_trace_event(
                "post_process_workflow_failed",
                {"reason": _REASON_WORKFLOW_FAILED, "error_count": len(state.get("error_log") or [])},
                state,
            )
            return _contain(_REASON_WORKFLOW_FAILED)

        formatted_output = {
            "message_id": state.get("message_id", ""),
            "message_ref": state.get("message_ref", ""),
            "message_preview": state.get("message_preview", ""),
            "intent": state.get("intent", ""),
            "confirmation": state.get("confirmation", ""),
            "teams_payload": from_json(state.get("teams_payload"), {}),
        }

        # Domain output gate (module-level helper - see module docstring).
        violations = _security_gate_output(formatted_output, is_success=True)
        if violations:
            # Containment (module docstring): ERROR alone leaves the un-gated
            # inner answer standing in `result`, and the base envelope ships it
            # as `formatted_output or result`. Clear every output-bearing field
            # and hand back the TRUTHY closed-set envelope; the violations go
            # to error_log (operator-side), never to the caller.
            emit_trace_event(
                "post_process_output_withheld",
                {"reason": _REASON_OUTPUT_WITHHELD, "violation_count": len(violations)},
                state,
            )
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_message_id": bool(state.get("message_id")),
            },
            state,
        )

        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
