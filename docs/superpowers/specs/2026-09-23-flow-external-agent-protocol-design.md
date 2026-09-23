# Flow Studio External Agent Protocol and Customer Agent Integration Design

Date: 2026-09-23
Status: approved in conversation; awaiting written-spec review

## 1. Goal

Flow Studio Agents support exactly three orchestration modes:

1. built-in ReAct;
2. an external agent provider;
3. a Flow graph.

The first external provider is Customer Agent (CA). Flow Studio owns the saved Agent configuration and sends a bounded capability selection to CA for each run. CA exposes a versioned Flow HTTP protocol, validates the requested capabilities, and applies them only to that run. It does not create or update a persistent CA Agent definition.

The protocol is provider-neutral so another external agent can implement it later. The current implementation, UI copy, tests, and operational support cover CA only.

## 2. Boundaries

- Flow Studio stores the external provider URL, a server-side credential reference, selected model profile, allowed Skills, allowed Tools, allowed MCP servers, and the memory switch.
- Browser code never calls CA directly. Flow Studio's backend performs catalog and run requests so credentials are not exposed and browser CORS is irrelevant.
- CA remains the owner of model credentials, Skill contents, Tool implementations, MCP connection details, runtime sessions, and Agent Loop execution.
- A Flow Agent configuration does not mutate CA settings or CA Agent definitions.
- The existing CA `/api/agent/run`, `/api/agent/stream`, settings, Skills, Tools, MCP, desktop, web-chat, and portfolio-content paths remain compatible.
- Existing Flow Agents using `runtime=local`, `runtime=customer-agent`, `profile_id`, `skill_ids`, or `flow_id` are migrated in memory to the new mode model and remain executable.
- Flow/Agent cycle checks and maximum nesting guards remain in force.

## 3. Flow Agent Model

The normalized Agent shape uses an explicit `orchestration` object:

```json
{
  "id": "job-agent",
  "name": "Job Agent",
  "orchestration": {
    "mode": "external_agent",
    "provider": "customer-agent",
    "connection": {
      "base_url": "http://127.0.0.1:3000",
      "credential_ref": "customer-agent-default"
    },
    "selection": {
      "model_id": "aihub-deepseek",
      "skill_ids": ["job-hunt"],
      "tool_ids": [],
      "mcp_server_ids": [],
      "memory_enabled": true
    }
  }
}
```

Mode-specific configuration is mutually exclusive:

- `react`: local Flow Studio prompt, knowledge bases, Skills, Tools, MCP, memory, and limits.
- `external_agent`: provider connection plus the catalog-backed selection above.
- `flow`: one referenced `flow_id` and no local or external capability selectors.

Credentials are stored by the Flow Studio backend and referenced by ID. The editor may replace or clear a token but never reads the stored value back. Agent list/detail responses expose only whether a credential is configured, never the secret value.

Legacy normalization maps `runtime=customer-agent` to `external_agent/customer-agent`, a non-empty `flow_id` to `flow`, and remaining local Agents to `react`. Saves write the new representation while retaining readers for historical revisions.

## 4. Unified HTTP Protocol

The protocol base path is `/api/flow/v1`. Requests use `Authorization: Bearer <token>` when the provider requires authentication. All responses include a stable machine-readable error code on failure.

### 4.1 Catalog

```http
GET /api/flow/v1/catalog
```

```json
{
  "protocolVersion": "1",
  "provider": {"id": "customer-agent", "name": "Customer Agent"},
  "features": {"streaming": true, "memory": true, "cancellation": true},
  "models": [{"id": "aihub-deepseek", "name": "DeepSeek", "provider": "aihub"}],
  "skills": [{"id": "job-hunt", "name": "job-hunt", "description": "..."}],
  "tools": [{"id": "browser_search", "name": "browser_search", "description": "...", "inputSchema": {}}],
  "mcpServers": [{"id": "local-mcp", "name": "Local MCP", "status": "available"}]
}
```

CA builds this response from its existing settings and business catalog. It omits model keys, Skill file paths and contents, MCP commands, environment variables, headers, tokens, and other connection secrets. The response is `cache-control: no-store`.

### 4.2 Start Run

```http
POST /api/flow/v1/runs
```

```json
{
  "input": "Find matching jobs",
  "agentId": "portfolio-content-agent",
  "sessionId": "flow-session-123",
  "context": {"flowRunId": "run-456", "nodeId": "agent-1"},
  "selection": {
    "modelId": "aihub-deepseek",
    "skillIds": ["job-hunt"],
    "activatedSkillIds": ["job-hunt"],
    "toolIds": ["browser_search"],
    "mcpServerIds": ["local-mcp"],
    "memoryEnabled": true
  }
}
```

`agentId` is optional. When present, CA applies the saved Agent's role prompt and treats its capabilities as an upper bound. When absent, CA uses its standard run prompt and the explicit selection is the complete capability policy.

`skillIds` is the Skill allowlist. `activatedSkillIds` is an optional subset that must be loaded before the first model call. A normal external Agent run sends the configured allowlist and lets CA discover the appropriate Skill. A Flow node that explicitly selects a Skill sends that ID in both arrays, preserving the current portfolio behavior.

Capability selection uses three-state semantics:

- a missing field uses CA's current default or selected persistent Agent policy;
- an explicit non-empty array is an allowlist;
- an explicit empty array allows none.

CA validates every selected ID before admitting the run. A selection never broadens the policy of a referenced persistent CA Agent; effective capabilities are the intersection of the persistent policy and the per-run selection. With no CA Agent ID, the explicit selection is the policy for that run.

The success response is:

```json
{
  "runId": "ca-run-123",
  "sessionId": "flow-session-123",
  "eventsUrl": "/api/flow/v1/runs/ca-run-123/events"
}
```

### 4.3 Events and Cancellation

```http
GET /api/flow/v1/runs/{runId}/events
POST /api/flow/v1/runs/{runId}/cancel
```

The event endpoint is SSE and emits normalized events with monotonically increasing IDs:

- `run.started`
- `assistant.delta`
- `tool.started`
- `tool.completed`
- `run.completed`
- `run.failed`

Each event contains `runId`, `sessionId`, `timestamp`, and a type-specific `data` object. `run.completed` contains the final assistant text and any public artifact references; Flow Studio treats that payload as the external Agent node output. `run.failed` contains a public error code and message. A privileged debug field may contain a full stack only when both services are in local/admin debug mode; normal provider responses and public UI never expose CA filesystem paths, secrets, prompts, or credentials.

CA may implement this endpoint as a compatibility adapter over its existing Agent events and `/api/agent/stream`. Flow Studio relays the normalized lifecycle into its existing live node event stream, so the canvas shows the active node, rolling logs, tool activity, and the Flow-side exception stack.

## 5. Customer Agent Changes

CA adds a focused Flow integration module rather than duplicating Agent Loop behavior:

1. A catalog route aggregates sanitized profiles, Skills, Tools, and MCP servers.
2. Request schemas validate size, type, duplicate IDs, unsupported features, and capability existence before run admission.
3. `SharedRunOptions` gains optional per-run Tool, Skill, activated-Skill, MCP, and memory policies.
4. `configureSharedRun` calculates the effective capability policy and wires it into a per-run `AgentBuilder` snapshot.
5. Tool and Skill filtering must distinguish an omitted policy from an explicit empty allowlist without changing legacy `AgentDefinition` empty-array semantics.
6. MCP assembly connects only effective allowed servers; an explicit empty list connects none.
7. `memoryEnabled=false` disables CA long-term memory access and memory tools for the run. Session transcript persistence remains available for execution and event replay. Flow sends a stable CA session ID only when conversational continuity is intended.
8. Existing Agent events are mapped to the protocol event names, and cancellation delegates to the existing active-run abort path.
9. Authentication and logging reuse CA's service-token boundary; request logs redact authorization, model secrets, MCP connection data, and sensitive context values.

The implementation must not temporarily modify shared settings or a persisted Agent definition. Every run receives an isolated builder/config snapshot so concurrent Flow runs cannot leak capability selections into one another.

## 6. Flow Studio Changes

Flow Studio adds an `ExternalAgentProvider` boundary with operations to fetch a catalog, start a run, consume events, and cancel a run. `CustomerAgentProvider` implements this boundary with the `/api/flow/v1` protocol. Provider errors are translated into Flow node errors without discarding their stable code.

The Agent editor replaces the current implicit runtime controls with a three-option segmented control: `ReAct`, `第三方智能体`, and `流程图`.

For `第三方智能体`, the first release provides one provider option, `Customer Agent`, and shows:

- CA address and credential status;
- a connection/catalog refresh action;
- one model-profile selector;
- searchable multi-select lists for Skills, Tools, and MCP servers;
- a memory toggle shown only when the catalog advertises memory support;
- independent loading, empty, stale-selection, authentication, and connection-error states.

The refresh action does not silently discard saved IDs. Missing catalog entries remain visible as stale selections until the user removes or replaces them. Saving is rejected when a required model is missing or a selected ID is known to be invalid.

At execution time, `AgentRuntime` delegates external Agents to the provider, subscribes to SSE, converts provider events into node progress/log events, and returns the final assistant output. It preserves the complete Flow-side exception chain for the run detail view while presenting a concise node error on the canvas.

## 7. Failure Handling

The protocol defines at least these error codes:

- `UNAUTHORIZED`
- `PROTOCOL_VERSION_UNSUPPORTED`
- `INVALID_REQUEST`
- `INVALID_CAPABILITY`
- `FEATURE_UNSUPPORTED`
- `SESSION_OCCUPIED`
- `PROVIDER_UNAVAILABLE`
- `RUN_FAILED`
- `RUN_CANCELLED`

Catalog failures do not erase the last saved selection. Run admission fails before model use if any requested capability is invalid. A dropped event stream retries with the last received event ID when CA supports replay; otherwise Flow marks the node failed instead of assuming completion. A terminal event is authoritative and is persisted with the Flow run.

Connection URLs must use HTTP or HTTPS and cannot include user information or fragments. Loopback CA may run without a token for local development; non-loopback addresses require a configured credential. Redirects are rejected by default to avoid forwarding credentials to another host.

## 8. Compatibility and Migration

- CA's existing `/api/agent/run` still accepts `profileId` and one `skillName` with the current validation rules.
- Existing CA Agent definitions continue to interpret empty capability arrays using their current defaults. The new explicit-empty semantics apply only to the versioned Flow protocol request.
- Existing Flow `portfolio-content-agent` data is normalized to the CA external-provider configuration and explicit Skill-node invocation continues to activate one selected Skill.
- Historical Flow revisions remain readable and executable.
- No automatic write is made to old revision files merely by listing or opening an Agent.
- The protocol version is fixed at `1`; incompatible future changes use a new versioned path.

## 9. Verification

CA tests cover catalog sanitization, authentication, model/Skill/Tool/MCP validation, missing-versus-empty selection semantics, persistent-policy intersection, per-run isolation, memory disabled behavior, MCP filtering, normalized event order, replay, cancellation, and legacy `/api/agent/run` behavior.

Flow tests cover legacy Agent normalization, three-mode validation, secret masking, catalog refresh and stale selections, provider request construction, explicit Skill activation, normalized SSE consumption, retry boundaries, cancellation, cycle prevention, and complete Flow-side failure persistence.

Browser verification covers desktop and mobile editor layouts, all three orchestration modes, CA connection and catalog refresh, model selection, multi-select capability controls, memory support states, saved configuration reload, and a real CA run whose active node and logs update live.

End-to-end acceptance requires a real local CA instance: fetch its sanitized catalog, run with an allowed Skill and Tool set, observe the normalized event stream through Flow Studio, verify the selected model is used, confirm an unselected capability is unavailable, and verify that neither service persisted a new CA Agent definition or leaked credentials into APIs or logs.

## 10. Out of Scope

- Supporting an external provider other than CA in the first release.
- Synchronizing or editing persistent CA Agent definitions from Flow Studio.
- Copying CA model keys, Skill files, Tool implementations, or MCP connection secrets into Flow Studio.
- Exposing arbitrary remote provider stack traces to public homepage users.
- Defining a public industry standard or guaranteeing compatibility outside this Flow Studio protocol.
