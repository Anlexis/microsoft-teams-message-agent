"""AgentCore Platform v1.0 - CMN-C2-234 Teams Message Agent state."""

# State contract: State must be a flat TypedDict - never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects (and
# nested dict/list containers) are not msgpack-safe. Extend AgentState with
# agent-specific fields only, and declare every domain field NotRequired[...]
# (fields are absent until their producer node writes them). teams_payload /
# teams_config / target_hint / redaction_flags are dicts/lists at the point of
# use but are stored in State as JSON strings via to_json/from_json below. Do
# NOT add credentials, secrets, or Pydantic models (PB-2 / PB-5). The Graph
# API integration token is NEVER stored here - it is read via ctx.secrets in
# CallTeamsApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Teams Message agent state.

    Shared fields (user_input, validated_input, intent, result, status,
    formatted_output, session_id, node_history, error_log, correlation_id,
    trace_id, hitl_*, etc.) are inherited from AgentState and NOT re-declared.
    Only Teams-workflow fields are added below, all NotRequired (state
    contract: fields are absent until their producer node writes them). All values are JSON/msgpack-serializable primitives -
    the Graph API integration token is NEVER stored here (accessed via
    ctx.secrets).
    """

    # Caller-supplied target hint (team/channel/chat ids from input_context /
    # the request envelope). Never inferred; resolution to a Teams target is
    # explicit-only. JSON dict {"team_id","channel_id","chat_id"} subset
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    target_hint: NotRequired[Optional[str]]

    # InferTeamsFields - resolved Teams target ids (explicit-only, never invented)
    team_id: NotRequired[str]  # Microsoft Teams team id
    channel_id: NotRequired[str]  # Microsoft Teams channel id
    chat_id: NotRequired[str]  # Microsoft Teams chat id

    # ValidateInput (deterministic redaction scan)
    # JSON list[str] of patterns redacted from the text before logging
    # (stored as a JSON string; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferTeamsFields
    message_preview: NotRequired[str]  # short preview of the resolved message body
    # JSON - assembled Microsoft Graph chatMessage request body (stored as
    # a JSON string, not a native dict; (de)serialize via to_json/from_json).
    teams_payload: NotRequired[Optional[str]]

    # Manifest `teams:` section forwarded by _parent_config() and injected
    # by the inner graph's _extra_initial_state() (JSON string).
    teams_config: NotRequired[Optional[str]]

    # CallTeamsApi
    message_id: NotRequired[str]  # message id returned by Microsoft Graph
    message_ref: NotRequired[str]  # human-readable reference (Graph webUrl / teams://... URI)

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message
