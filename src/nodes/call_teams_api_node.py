"""AgentCore Platform v1.0 - inner workflow Step 4: CallTeamsApi (tool side-effect).

Performs the channel/chat message post against the Microsoft Graph API v1.0
Teams message endpoints via src/services/teams_client.py.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives on
       the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner node.
       GraphNode.execute() passes the caller's InvocationContext into the inner
       subgraph UNCHANGED (no trust elevation), so a real external caller runs
       this call under its own VERIFIED_EXTERNAL context; declaring INTERNAL here
       would trust-deny that already-gated external caller before the call
       ever runs. The node therefore stays ANONYMOUS.
  Secrets: the integration token is read via ctx.secrets.get("TEAMS_GRAPH_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored in
       state. v1 note: while the deterministic NETWORK-FREE stub transport is
       active a missing token is tolerated (a sentinel placeholder is used - it
       is never sent anywhere because no request leaves the process); with a
       LIVE transport injected, a missing token is a hard status=error - a real
       API is never called unauthenticated. The manifest declares no required
       secret (requires.secrets: []) because the shipped stub transport runs
       without one; a live deployment provisions TEAMS_GRAPH_TOKEN through the
       secrets provider.
  Audit: emit_trace_event() is called on the success path - a side-effect against
       an external collaboration system; HTTP 4xx/5xx surfaces as status=error +
       error_log (no silent pass).

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). Teams settings (base_url) arrive as the JSON `teams_config` state
field - injected by the inner graph's _extra_initial_state() from the
config/config.yaml `teams:` section forwarded by
TeamsWorkflowGraphNode._parent_config() - or via the optional
`config["configurable"]["teams"]` argument for direct invocation.
The client is constructed locally per call (no module-global mutation).
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.teams_client import TeamsApiError, TeamsClient

_SECRET_KEY = "TEAMS_GRAPH_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"


class CallTeamsApiNode(FunctionNode):
    """Post a channel / chat message via the Microsoft Graph API v1.0."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = from_json(state.get("teams_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallTeamsApiNode: missing teams_payload"],
            }

        intent = state.get("intent", "post_channel_message") or "post_channel_message"

        # Explicit-body-only guard: never post an empty message.
        content = str(((payload.get("body") or {}).get("content")) or "").strip()
        if not content:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallTeamsApiNode: no message body resolved - " "refusing to post an empty message"],
            }

        # Settings: config/config.yaml section from state (graph-injected), overridable via
        # an explicit config["configurable"]["teams"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("teams_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("teams") or {}
        settings.update(override)

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE v1 stub (documented limitation, docs/02).
        base_url = str(settings.get("base_url", "") or "").strip()
        client = TeamsClient(base_url=base_url) if base_url else TeamsClient()

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # v1 stub limitation: no request leaves the process, so run with
                # a non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        f"CallTeamsApiNode: secret {_SECRET_KEY} unavailable - "
                        "refusing to call a live transport unauthenticated"
                    ],
                }

        team_id = state.get("team_id", "") or ""
        channel_id = state.get("channel_id", "") or ""
        chat_id = state.get("chat_id", "") or ""

        try:
            if intent == "post_channel_message":
                # Explicit-target-only: an unresolved target is an error, never a guess.
                if not team_id or not channel_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": [
                            "CallTeamsApiNode: unresolved team_id/channel_id - " "cannot post a channel message"
                        ],
                    }
                resp = client.post_channel_message(team_id, channel_id, payload, api_token) or {}
            elif intent == "post_chat_message":
                if not chat_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallTeamsApiNode: unresolved chat_id - " "cannot post a chat message"],
                    }
                resp = client.post_chat_message(chat_id, payload, api_token) or {}
            else:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"CallTeamsApiNode: unknown intent '{intent}'"],
                }
        except TeamsApiError as exc:
            # Closed-set labels only: the exception's message carries the
            # upstream Graph error body, which can name people, tenants or
            # tokens. Record the class and the HTTP status - never the text.
            status_code = exc.status_code
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallTeamsApiNode: TeamsApiError status={status_code}"],
            }
        except Exception as exc:  # transport failure - no silent pass
            # Class name only - a transport exception's message can embed the
            # request URL, the response body or the failing host.
            exc_class = type(exc).__name__
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallTeamsApiNode: Teams call failed: {exc_class}"],
            }

        message_id = str(resp.get("id", "") or "")
        message_ref = str(resp.get("webUrl", "") or "")
        if not message_ref and message_id:
            if intent == "post_chat_message":
                message_ref = f"teams://chats/{chat_id}/messages/{message_id}"
            else:
                message_ref = f"teams://teams/{team_id}/channels/{channel_id}/messages/{message_id}"

        # Audit the tool side-effect - intent + presence signals only,
        # never message content or credentials.
        emit_trace_event(
            "call_teams_api_complete",
            {
                "intent": intent,
                "has_message_id": bool(message_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "message_id": message_id,
            "message_ref": message_ref,
            "message_preview": state.get("message_preview", "") or content[:80],
            "status": AgentStatus.SUCCESS.value,
        }
