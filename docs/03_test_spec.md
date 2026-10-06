# Test Specification - CMN-C2-234 Teams Message Agent

## Test Strategy
- Test types: Unit (per node + service + config + inner graph) / Proof-of-Boundary
  (full outer-graph invoke, ASGI endpoint end-to-end, blocked-response
  containment, import isolation, state safety, server boot, HITL stub).
- Location: `tests/unit/`, `tests/proof_of_boundary/` (`tests/integration/` is an
  empty package; end-to-end backbone coverage lives in PB-6, PB-8 and PB-9, which
  drive the real compiled outer graph, the real ASGI `/invoke` entry, and the
  caller-facing envelope a blocked response produces).
- The Microsoft Graph call is exercised through the deterministic, network-free v1
  stub transport (default) and through monkeypatched fake clients; no live Graph call.
- **Trust-gate routing canon**: every per-node unit test invokes the node as
  `node(state)` - `BaseNode.__call__` routes the full security pipeline (trust
  gate -> input PII mask -> `execute()` -> output credential scan) - never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode (the single external gate)
  and `TrustLevel.ANONYMOUS.value` for every other node. The single documented
  exception: `CallTeamsApiNode.execute(state, config=...)` config-override test
  (a 2nd argument `__call__` cannot forward). The trust-rejection test asserts on
  the RETURNED error dict (`status == AgentStatus.ERROR.value`, "trust gate denied"
  in `error_log`, execute-only keys absent) - `__call__` never raises for a
  trust denial.
- Assertion contract (framework wheel `agenticstar-agentcore==1.0.3`): the invoke
  surface is `result["output"]` / `status` / `trace_id` / `correlation_id` /
  `node_history` (never `formatted_output` at the invoke surface); status is
  compared to `AgentStatus.SUCCESS`/`.value` (lowercase `success`/`error`); the
  outer graph is called as `invoke(user_input=..., ctx=...)`; identifiers may be
  masked (`[MASKED]`) so message evidence is asserted by presence, not raw repr;
  audit spies assert on `call.args[1]` (the event payload), never the whole-call
  repr.
- Real-SDK pipeline behaviors encoded by the suite: `__call__` short-circuits on an
  incoming errored state (execute() is skipped; error status/error_log pass through);
  the framework input mask rewrites Title-Case bigrams (across newlines), emails, and
  digit groups in `user_input`/`validated_input` to `[MASKED]` before `execute()`
  sees the text - positive payloads are PII-free, intentional-PII tests assert the
  `[MASKED]` path. Teams channel-style ids (`19:...@thread.tacv2`) do NOT match the
  email pattern (digit-tailed TLD) and survive the mask (asserted explicitly);
  real-style chat ids (`19:...@unq.gbl.spaces`) WOULD match, so chat targets travel
  via the validated `input_context` hint and the graph bridge
  (`src/graph/context_bridge.py`) - pinned end-to-end by PB-8's chat-hint test.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; inner nodes ANONYMOUS; trust-posture declarations | denial RETURNS error dict (`status == AgentStatus.ERROR.value`, "trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL passes; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | caller-data contract: serialize NL request + Teams target hint (`team_id`/`channel_id`/`chat_id` from input_context) into `validated_input` (JSON); HTML strip; blank hints dropped; hostile-hint matrix (wrong type / wrong charset / over-length / non-dict input_context) fails CLOSED with field-named errors, value never echoed | valid hint collected verbatim (never inferred); `<script>` stripped; empty/missing -> `status=error`; malformed hint -> `status=error` naming `input_context.<field>` only; 80-char boundary id accepted; audit payload carries hint presence only |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; channel-style id survives the masks; node-level token flag-and-redact (`secret_*`); state target hint precedence over the embedded copy | email -> `[MASKED]` before execute; `19:...@thread.tacv2` unmasked; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short -> error; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = post_channel_message / post_chat_message (keyword, chat-first priority, channel default with note) | correct intent per keyword (EN + JA); chat signal wins; no-signal defaults to post_channel_message with non-fatal note; empty -> error; audit emits intent label only |
| U-05 | test_infer_teams_fields_node.py | explicit-only target resolution (text mention > id-shaped hint > id-shaped KV line; never invented); quoted-after-verb / `Message:` body (never the whole text); Microsoft Graph chatMessage request body (JSON string) | team/channel/chat ids extracted; unresolved target/message left `""`; non-id-shaped hint ignored; empty input -> error; audit emits presence signals only |
| U-06 | test_call_teams_api_node.py | channel/chat post via the network-free v1 stub; `teams_config` state field + `execute(state, config=...)` override (documented direct-execute exception); API error / empty body / unresolved target / unknown intent / missing payload; secret posture (live transport refuses to run unauthenticated; token read via `ctx.secrets`, never env/state) | message_id/message_ref on success; 403 surfaces in error_log; empty body -> "refusing to post"; live+no-secret -> error "unauthenticated"; live+bound secret -> token passed to client; audit emits presence signals with `stub_transport=True` |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; ref/id formatting; preview fallback | "Posted channel/chat message ... ref=... id=..."; unknown intent -> generic verb; missing evidence -> error |
| U-08 | test_post_process_node.py | `formatted_output` shaping (JSON payload round-trip); errored state passes through `__call__` un-masked (short-circuit); output gate (module-level `_security_gate_output()` helper, full-node path + direct function tests): message-evidence rule, credential scan WALKING NESTED structures (top-level control + nested `teams_payload.body.content` leak + list leak), the credential classes the framework scan recognises (AWS key id / Stripe key / connection string), violations name the field path never the value with unknown path components masked (control: a known field name IS named); ERROR envelope is closed-set on every non-success path (errored state / missing evidence / nested credential, parametrised): keys and values drawn from the module constants, truthy, output-bearing fields cleared, an `error_log` sentinel (name + id + token-shaped text) reaches nowhere in the returned mapping, gate violations land in `error_log` never in the envelope, errored path re-emits nothing | success shape with parsed `teams_payload`; error status/error_log preserved, no success shape fabricated; SUCCESS without message_id/message_ref blocked; nested credential blocked and the response replaced by the truthy closed-set envelope `{withheld, reason}` |
| U-09 | test_teams_client.py | Microsoft Graph v1.0 client: POST /teams/{id}/channels/{id}/messages + POST /chats/{id}/messages; Bearer auth header; `TeamsApiError` on non-2xx; stub shapes (deterministic id + webUrl, `_stub` marker); `uses_stub_transport` | correct URLs/headers/bodies; 403/400 raise with the Graph error message; stub deterministic shapes |
| U-10 | test_config.py | flat `config/agent.yaml` manifest sanity + `config/config.yaml` runtime sanity | id CMN-C2-234, name, namespace `cmn`, Cat 2, CMN, ToolCallingAgent, root-level `class` dotted path (no `agent:` nesting), `generation_mode: deterministic`, VERIFIED_EXTERNAL, `requires.secrets == []` and `requires.extras == []` (the token is an optional runtime secret - see docs/02), `teams.base_url`, integer `max_retry`/`timeout_s` |
| U-11 | test_domain_workflow_graph.py | inner `TeamsWorkflowGraph`: identity, `_extra_initial_state()` teams_config JSON injection, `route()` error short-circuit, `get_output` contract, compile, direct inner invoke on the stub | name/state_schema correct; config forwarded as JSON string; error -> END; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with message evidence |
| U-12 | test_config_arrival.py | declared config ARRIVES at its consumer: `_parent_config()` reads the LIVE `config/config.yaml`; file -> parent config -> inner state; full outer invoke proves the declared `base_url` reaches the constructed Teams client; server-loaded `max_retry`/`timeout_s` reach the graph config; missing file degrades to safe defaults | forwarded values equal the file's values; recording client captures the declared base_url on a real invoke; absent file -> `{"configurable": {}}`, no crash |
| U-13 | test_framework_compliance_tc06_tc07.py | framework gate finality: overriding the default input/output gates raises at class definition | TC-06/TC-07 raise; domain checks live in `_extra_*` hooks or module-level helpers only |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: 0 platform-internal imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external-trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, TeamsWorkflowGraphNode, PostProcessNode, FinalizeNode]` and message evidence + confirmation in `result["output"]`; ANONYMOUS caller denied at pre_process (error, no post_process, no output); blank input -> error, not crash |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (no graph class declares `propagate_hitl=True`; no cross-boundary interrupt() checkpoint): skip stub with a real AssertionError body - enabling HITL propagation without implementing PB-7 fails loudly |
| PB-8 | ASGI `/invoke` end-to-end | test_pb_invoke_endpoint.py | Bearer auth (missing/wrong token -> HTTP 401, generic body); authenticated channel post -> SUCCESS with message evidence; chat-id hint crosses the graph bridge intact (the id the framework mask would rewrite in text); runtime config values visible on the served graph; malformed hints -> error, field named, value never echoed; non-object input_context -> 422; >256KB input_context -> 413; steering text with no explicit target -> error, nothing published; benign request with similar words unaffected; whole nested response free of credential-shaped strings and grouped-digit artifacts |
| PB-9 | Blocked-response containment | test_output_containment.py | Real backbone run (real pre_process + real main slot + real inner graph on the stub transport), then post_process through the full node pipeline, then the agent's own `get_output()`: a blocked response yields `status=error` with a TRUTHY withheld notice as `output`, and the caller's envelope carries no released text, no traceback, no source path and no message reference; the state the gate leaves behind has `result`/`message_preview`/`confirmation`/`teams_payload`/`message_id`/`message_ref` cleared. Three blocks driven - missing message evidence, credential nested in the payload, and a credential class the framework scan would otherwise raise on (asserted traceback-free, proving the block happened in the gate and not in the node wrapper). A clean-path control on the same run must still deliver the real confirmation + message evidence, so a refuse-everything gate cannot pass. On every non-success path (inner-workflow error driven through `execute()` since the pipeline short-circuits it; both gate blocks through the full pipeline) the caller's `output` is exactly the closed-set envelope `{withheld, reason}`, and an `error_log` sentinel (name + id + token-shaped text) reaches nowhere in the caller's envelope |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); agent constructs + compiles via the supported no-arg-ctor + compile() path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy
> tests (pre_process / validate / classify / infer / call nodes assert on the
> event payload, `call.args[1]`). PB-3 (live external service) is exercised at
> deployment first-invoke, not in this suite - the v1 transport is the
> documented network-free stub.

## Test Execution Summary
- Execution date: 2026-08-31
- Runner: pytest under the real framework wheel `agenticstar-agentcore==1.0.3`
- Total tests: 197
- Pass: 196 / Fail: 0 / Skip: 1 (PB-7 - auto-waived, non-HITL)
