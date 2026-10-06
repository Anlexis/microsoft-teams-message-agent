"""AgentCore Platform v1.0 - caller-context bridge across the graph boundary."""

# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` and does NOT forward
# outer state fields, so the caller's validated Teams target hint
# (state["target_hint"], collected from input_context by PreProcessNode) would
# never reach the inner workflow on its own. The sanctioned subclass hooks
# bridge it:
#
#   TeamsWorkflowGraphNode.extract_input(state)   [runs BEFORE subgraph.invoke]
#       -> set_caller_target_hint(state["target_hint"])
#   TeamsWorkflowGraph._extra_initial_state()     [runs INSIDE subgraph.invoke]
#       -> returns {"target_hint": <stashed hint as JSON>}
#
# What crosses the bridge is the VALIDATED caller contract produced by
# PreProcessNode - every id already passed the bounded Teams-id shape check -
# never the raw request body.
#
# The alternative, smuggling the hint inside the validated_input JSON, is not
# reliable here: the framework masks that field at node boundaries, and a
# real chat id (19:...@unq.gbl.spaces) matches the email pattern - the inner
# graph would receive "19:[MASKED]" and refuse a target the caller named
# correctly. This channel is not masked.
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's hint.

from contextvars import ContextVar

_CALLER_TARGET_HINT: ContextVar["dict[str, str] | None"] = ContextVar("cmn_c2_234_caller_target_hint", default=None)


def set_caller_target_hint(hint: "dict[str, str] | None") -> None:
    """Stash the validated target hint for the imminent inner-graph invoke."""
    _CALLER_TARGET_HINT.set(dict(hint) if hint else {})


def get_caller_target_hint() -> "dict[str, str]":
    """Read (without consuming) the stashed hint; {} when none was set."""
    return _CALLER_TARGET_HINT.get() or {}
