# Template Design Specification — CMN-C2-234 Teams Message Agent

## Position in AgentCore Architecture

- **Agent Class**: `TeamsMessageAgent` (`src/graph/graph.py`)
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
- **Category**: Cat 2 (multi-step domain workflow, ToolCallingAgent). Outer
  `AgentBaseGraph` 5-node backbone; the domain pipeline is encapsulated in a
  `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
  (`src/graph/domain_workflow_graph.py`).
- **Agent Type**: ToolCallingAgent — classify intent -> extract the message
  body + Teams target (team/channel/chat) -> build a Microsoft Graph API
  request -> call the tool -> format the confirmation. No RAG retrieval, no
  autonomous ReAct loop.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — msgpack incompatible)
  - Node: framework base-class inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
    not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | validate the caller contract (every `input_context` hint checked against the bounded Teams-id shape, fail closed, field-named errors); HTML/length sanitize; serialize NL request + target hint into `validated_input` (JSON) | user_input, input_context | validated_input, target_hint | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner Teams workflow subgraph | validated_input | result, intent, team_id, channel_id, chat_id, message_id, message_ref, message_preview, confirmation, teams_payload | GraphNode (caller ctx forwarded unchanged) | TeamsWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; module-level `_security_gate_output()` scan (walks the NESTED output) | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

Inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline
steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------------|
| validate_input | 1 ValidateInput | empty/non-request guard; deterministic (regex) flag-and-redact of email/token-like strings before logging; state-provided target hint takes precedence over the copy embedded in the serialized request (the framework mask can rewrite the embedded copy) | validated_input, target_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification -> post_channel_message / post_chat_message; low-confidence -> post_channel_message with a note (safety is enforced by the explicit-target-only rule — an unresolved target is a status=error, nothing is ever posted) | intent | ANONYMOUS |
| infer_teams_fields | 3 InferTeamsFields | extract the message body + explicit team/channel/chat ids; assemble the Microsoft Graph chatMessage request body per intent; an unresolved target id is left empty (never invented) | team_id, channel_id, chat_id, message_preview, teams_payload | ANONYMOUS |
| call_teams_api | 4 CallTeamsApi | POST /teams/{team-id}/channels/{channel-id}/messages (channel) / POST /chats/{chat-id}/messages (chat) via `TeamsClient`; token via ctx.secrets; 4xx/5xx -> status=error | message_id, message_ref, message_preview | ANONYMOUS |
| confirm | 5 Confirm | format intent + message id + reference into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / TeamsWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_teams_fields
              -> call_teams_api -> confirm -> END
```

Structured parameters travel on two channels. The TEXT rides as a JSON
string: `pre_process` serializes `{"text", "target_hint"}` into
`validated_input`, `TeamsWorkflowGraphNode.extract_input()` hands that JSON
to the subgraph, and the first inner node (`validate_input`) parses it back.
The VALIDATED target hint additionally crosses on the graph bridge
(`src/graph/context_bridge.py`): `extract_input()` stashes it in a
ContextVar and the inner graph's `_extra_initial_state()` seeds it into
inner state — necessary because the framework's PII mask rewrites
`validated_input` at node boundaries, and a real chat id
(`19:...@unq.gbl.spaces`) matches the email pattern, so a hint that rode
only inside the serialized JSON could arrive masked and unusable. The state
field wins over the embedded copy in `validate_input`.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (state contract —
fields are absent until their producer node writes them). Dict/list payloads
are stored as JSON strings (`Optional[str]`) via the module helpers
`to_json` / `from_json`, used by every producer and consumer.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| target_hint | NotRequired[Optional[str]] | JSON — caller-supplied target hint (`{"team_id","channel_id","chat_id"}` subset from input_context), every value validated against the bounded Teams-id shape; never inferred (stored as a JSON string via `to_json`/`from_json`; crosses the graph boundary on the context bridge) | pre_process / validate_input / inner graph |
| team_id | NotRequired[str] | resolved Teams team id (v1: pass-through when the hint/text already carries an explicit id) | infer_teams_fields |
| channel_id | NotRequired[str] | resolved Teams channel id | infer_teams_fields |
| chat_id | NotRequired[str] | resolved Teams chat id | infer_teams_fields |
| redaction_flags | NotRequired[Optional[str]] | JSON list of redaction patterns applied before logging | validate_input |
| message_preview | NotRequired[str] | short preview of the resolved message body (confirmation label) | infer_teams_fields / call_teams_api |
| teams_payload | NotRequired[Optional[str]] | JSON — assembled Microsoft Graph chatMessage request body (msgpack-safe: stored as a JSON string via `to_json`/`from_json`) | infer_teams_fields |
| teams_config | NotRequired[Optional[str]] | JSON — `config/config.yaml` `teams:` section forwarded by `_parent_config()` and injected via the inner graph's `_extra_initial_state()` | inner graph |
| message_id | NotRequired[str] | message id returned by Microsoft Graph | call_teams_api |
| message_ref | NotRequired[str] | human-readable message reference (Graph `webUrl`, else `teams://…/messages/<id>`) | call_teams_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from
`AgentState` and are **not** re-declared.

**State Constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the Graph API token is accessed via `ctx.secrets`.
- InvocationContext read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration (flat manifest + runtime file)

`config/agent.yaml` is the FLAT registry manifest — identity, entry point
(`class: "src.graph.graph.TeamsMessageAgent"`), trust requirement and the
compile-time `requires:` gates, every key at root level. Runtime parameters
live in `config/config.yaml`:

- `max_retry` / `timeout_s` — consumed by the framework through the graph
  constructor; the standalone server (`src/api/server.py`) loads the file and
  passes it as `TeamsMessageAgent(config=...)`.
- `teams:` (`base_url`) — the Microsoft Graph integration section.

Nodes take **no constructor arguments** (SDK v1 nodes are no-arg; configuration
never rides on node instances). `TeamsWorkflowGraphNode._parent_config()`
loads `config/config.yaml` and forwards the `teams:` section (and `llm:` if
ever declared) to the inner graph under `config["configurable"]` — never `{}`
(an empty parent config would silently turn every declared setting into its
default). The inner graph's `_extra_initial_state()` then injects the `teams`
section into State as a JSON string (`teams_config`), where
`CallTeamsApiNode.execute(state, config=None)` reads it (an explicit
`config["configurable"]["teams"]` override is also honoured for direct/unit
invocation). The runtime scalars are deliberately not re-forwarded to the
inner graph — their consumer is the outer framework run loop. Declared-value
arrival is pinned by tests end-to-end (file -> parent config -> inner state ->
the constructed Teams client).

## Structured Product (`get_output()` override)

`TeamsMessageAgent.get_output()` **extends** `super().get_output()` (never
replaces the base envelope — `output`/`status`/`trace_id`/`correlation_id`/
`node_history` are preserved) and surfaces the structured product keys
(`message_id`, `message_ref`, `intent`, `confirmation`) **only when the run
ended SUCCESS**. On any non-SUCCESS state the structured keys are withheld
(fail-closed — the post_process gate has already errored any SUCCESS
response that lacks message evidence, so no unverified message reference can
ever reach the caller).

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner
  domain node — **including the write-capable `CallTeamsApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary
  is therefore enforced exactly once, at `pre_process`. Agent-level default trust
  `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`. `src/api/server.py`
  enforces the standalone entry-point Bearer-token auth boundary
  (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation) and caps the
  `input_context` envelope at 256 KB (HTTP 413).
- **Caller-data contract** — `pre_process` owns it: `input_context` must be an
  object; each recognised hint (`team_id` / `channel_id` / `chat_id`) must be a
  string matching the bounded Teams-id shape (`[A-Za-z0-9][A-Za-z0-9:@._-]{0,79}`,
  single definition in `src/services/security.py`); a present-but-malformed value
  is a hard `status=error` that names the FIELD and never echoes the value
  (fail closed — silently dropping a hint could post to a different target than
  the caller intended); unknown keys are ignored. The pipeline carries no
  caller-controlled numeric field (inventory by grep: `input_context` is read
  only in `pre_process`, and the three hints plus the free-text request are the
  entire caller surface), so no numeric-bounds parser is required — if a numeric
  field is ever added, it must go through a finite+bounded parser.
- **Input flag-and-redact** — `ValidateInputNode.execute()` runs a
  deterministic (regex, NOT LLM) scan for email addresses and access-token-like
  strings (`eyJ...`, `secret_...`, `sk-...`) and redacts them before any
  logging. A Teams message request legitimately names people and targets, so
  this is flag-and-redact for safe logging, not a hard reject; the framework
  input-side PII mask in `BaseNode.__call__` additionally masks
  emails/phones/names in `user_input`/`validated_input`. The only
  deterministic auto-reject is the empty/non-request guard. Write safety does
  not depend on text screening at all: both intents are writes, and the
  explicit-target-only rule means nothing posts without a caller-named target
  and a caller-quoted body — steering text with no explicit target is refused
  because there is nothing to post and nowhere to post it.
- **Secrets + output gate** — the integration token is read via
  `ctx.secrets.get("TEAMS_GRAPH_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. **The manifest declares
  `requires.secrets: []`** — a deliberate, visible contract choice: the token
  is read through the OPTIONAL `get()` accessor because the shipped network-free
  stub transport runs without a credential, and declaring a secret the runtime
  does not provision would fail the agent at compile time. A live deployment
  provisions `TEAMS_GRAPH_TOKEN` through the secrets provider without a manifest
  change; with a live transport injected, a missing token is a hard
  `status=error` — a real API is never called unauthenticated. The domain output
  gate is the **module-level** `_security_gate_output()` in
  `src/nodes/post_process_node.py`, called from `PostProcessNode.execute()`: it
  blocks any SUCCESS response that lacks message evidence
  (message_id/message_ref) and any credential-shaped string **anywhere in the
  nested output** — the gate walks dicts/lists/tuples, because the caller-facing
  output embeds the assembled chatMessage payload as a nested mapping and a
  token one level down must be caught exactly like a top-level one. It
  recognises credentials with the framework's own `detect_credentials()`, so a
  string this gate passes can never trip the framework output scan that runs
  immediately after it. Violations name the field path (path components outside
  the known output field names are masked), never the value. The
  caller-visible ERROR envelope is a **closed set**: on every non-success path
  (inner-workflow error, gate violation) `formatted_output` is exactly
  `{"withheld": true, "reason": <code>}` with `reason` one of the module
  constants `workflow_failed` / `output_withheld` — nothing is read from
  `error_log`, the violations list, a message id/ref or an exception message,
  because those can carry identifiers, names and upstream response bodies that
  truncation or credential-only redaction does not bound. `error_log` stays
  operator-side; the base envelope never projects it. No
  node defines `_extra_security_gate_input/_output` instance methods (framework
  hooks are @final / auto-wrapped — domain checks live inline or in
  module-level helpers).
- **Blocked-response containment** — a blocked response releases nothing.
  The base envelope resolves the caller-facing output as
  `formatted_output or result`, with no status check, so returning
  `status=error` alone would still hand the caller the un-gated inner answer
  inside the error envelope (and a node that raises returns a bare error
  partial that clears nothing). On a gate violation `PostProcessNode.execute()`
  therefore clears every output-bearing field — `result`, `message_preview`,
  `confirmation`, `teams_payload`, `message_id`, `message_ref` — and returns a
  **truthy** withheld notice as `formatted_output`; a falsy stub (`""`, `{}`)
  would re-activate the `or result` fallback and leak by another route. The
  detail stays operator-side (`error_log` + the `post_process_output_withheld`
  audit event, which carries the reason code and a violation count only); the
  caller sees the closed-set envelope. An inner-workflow error is contained by
  the same helper (`reason: workflow_failed`). Pinned end-to-end by PB-9,
  which asserts the caller's envelope carries no released text, no traceback
  and no source path, against a clean-path control that must still deliver the
  real confirmation.

- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-PII payload}, state)` on its
  SUCCESS path (intent / presence signals only — never request text, message
  content, or credentials). `__call__()` is never overridden; `_invoke_impl`
  is never defined on any node. Event names (documented for operations):

  | Node | Audit event |
  |------|-----------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_teams_fields | `infer_teams_fields_complete` |
  | call_teams_api | `call_teams_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete` |

## Output Invariant — decision record

The template's stated output invariant is: **a SUCCESS response always carries
verifiable message evidence (`message_id`/`message_ref`), and no
credential-shaped string reaches the caller in any representation** — enforced
by the nested-walking gate above and pinned both ways by tests (nested leak
probe AND a top-level control that proves the scanner itself fires).

A numeric precision grid (rounding rendered aggregates) is **not applicable**
here, on evidence rather than assumption: the response renders no monetary or
aggregate numbers at all — its numeric content is Microsoft Graph message ids,
which are pure-numeric identifier strings (epoch-millisecond style, e.g.
`140885145081096`), plus ids embedded in `webUrl` references. A digit-grouping
grammar would have nothing legitimate to protect and would corrupt exactly
these identifiers (a pure-numeric id has no letters for an identifier guard to
anchor on). The identifier-integrity property is pinned instead: the
end-to-end suite asserts the response carries no grouped-digit artifact
(`\d,\d{3}`) anywhere.

## v1 Implementation Note — LLM synthesis

v1 is fully deterministic: intent classification (`ClassifyIntentNode`) uses a
keyword heuristic and field inference (`InferTeamsFieldsNode`) uses regex/
line-structure extraction, so the template runs and tests without a live LLM.
**No LLM client is constructed anywhere in v1** and no `system_prompt` is read
(no dead config). LLM-backed synthesis (richer intent classification,
free-text-to-field mapping, natural-language message drafting) is a documented
follow-up: the manifest `llm:` section — when declared — is already forwarded
to the inner graph by `_parent_config()`, so wiring an LLM in is additive and
requires no graph-shape change.

## v1 Limitation — Teams client (documented)

`src/services/teams_client.py` follows the injectable-transport service shape
(injectable transport, `TeamsApiError`, per-call token, no
framework imports) and ships a **deterministic, network-free v1 stub** as its
default transport: it returns the documented Microsoft Graph chatMessage
response shape (an `id` + `webUrl` derived deterministically from the request)
so the pipeline is runnable and testable without a live Microsoft 365 tenant
or the `requests` package. It does **not** perform a live Graph call. This
follows the rule: never fake a live call — document the limitation.

A live Microsoft Graph write is **not a v1 capability** — this is the
deliberate delivered scope, not a doc-vs-code gap: `CallTeamsApiNode`
constructs `TeamsClient` without a transport on every v1 path, so the stub is
always selected (`uses_stub_transport == True`, surfaced operationally in the
`call_teams_api_complete` audit event's `stub_transport` field). The
constructor's injectable `post` transport is the **construction-time
deployment-integration seam**: injected transports are exercised by the test
suite today, and going live is a transport injection at deployment
integration — the method contracts and payload shapes are already Microsoft
Graph v1.0 exact (`POST /teams/{team-id}/channels/{channel-id}/messages`,
`POST /chats/{chat-id}/messages`), so no business-logic change is required.
(The stub also runs without a live credential — see "Secrets + output
gate" above; a live transport requires `TEAMS_GRAPH_TOKEN`, refused as a
hard `status=error` when missing.)

## Framework Utilization

### Shared Components Used
- [x] InvocationContext — read in `CallTeamsApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallTeamsApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("TEAMS_GRAPH_TOKEN")` (optional accessor; `requires.secrets: []` — see "Secrets + output gate"); entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] Audit `emit_trace_event()` — one positional call per node on the SUCCESS path; framework lifecycle events (node_start/node_complete/node_error/s1_denied) NOT re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `TeamsWorkflowGraph` (`BaseGraph`) via `TeamsWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `TeamsWorkflowGraphNode._parent_config()` loads
  `config/config.yaml` and forwards `{teams, llm (if declared)}`
  under `config["configurable"]` to the subgraph; the caller's validated
  target hint crosses on the context bridge (`src/graph/context_bridge.py`).
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as
  `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures
  (no silent pass).

## Import Isolation Confirmation
- [x] Template imports `framework/` and `shared/` only; no platform-internal SDK import anywhere
- [x] `src/services/teams_client.py` and `src/services/security.py` have no
      framework imports (pure service layer, stdlib only)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat Cat 1 (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat — `gate-composition`; 5 domain steps live in the inner graph |
| LLM dependency | LLM client in v1 | deterministic v1, LLM as documented follow-up | deterministic v1 | template runs/tests without a live LLM; no dead prompt/config reads; see "v1 Implementation Note — LLM synthesis" |
| Teams client | live `requests` call | injectable transport + documented v1 stub default | injectable + v1 stub default | no live network in v1; document the limitation; go-live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + manifest forwarding via `_parent_config()` -> `configurable` -> state | no-arg nodes | SDK v1 nodes are no-arg (ctor args TypeError at graph build); manifest stays the single config source |
| Write target | infer team/channel/chat from NL freely | caller-supplied/explicit ids only; unresolved left empty | explicit only | never post to the wrong channel or chat; unresolved target -> status=error, not invented |
| Message body | fall back to the whole request text | explicit body only (quoted / `Message:` line); empty -> error | explicit only | never post noise — an unresolvable body is a status=error, nothing is sent |
| Low-confidence intent | status=error | post_channel_message + non-fatal note | post_channel_message + note | both intents are writes, so the write-safety control is the explicit-target-only rule (nothing posts without a caller-named target); channel posting is the primary use-case |
| Structured product | replace get_output() | extend `super().get_output()`; structured keys on SUCCESS only | extend, SUCCESS-only | base envelope (status/trace/history) preserved; fail-closed — no message reference surfaces on a gated/failed run |
