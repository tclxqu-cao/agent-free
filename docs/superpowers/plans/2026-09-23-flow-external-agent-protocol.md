# Flow External Agent Protocol Implementation Plan

> **For the main agent:** Implement this plan directly in the current session. Do not dispatch implementation or code-review subagents. After all development tasks are complete, run the affected unit tests and fix any failures before reporting completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three explicit Flow Agent orchestration modes and integrate Customer Agent through a versioned catalog, run, event, and cancellation protocol with per-run model, Skill, Tool, MCP, and memory selection.

**Architecture:** Customer Agent exposes `/api/flow/v1` as an additive adapter over its existing catalogs, AgentHost, and event bus. Flow Studio owns external Agent configuration and credentials, calls CA through a provider boundary, and relays provider events into its existing node logs and run records. Existing Flow and CA Agent data remains readable and existing CA endpoints retain their behavior.

**Tech Stack:** TypeScript, Next.js route handlers, Bun/Vitest, Python 3, FastAPI, httpx, vanilla JavaScript/CSS, pytest.

## Global Constraints

- The editor exposes exactly `ReAct`, `第三方智能体`, and `流程图` orchestration modes.
- The first external provider is `customer-agent`; the provider boundary remains extensible.
- Flow Studio stores selections and a credential reference; it never stores CA catalog secrets in Agent JSON.
- Per-run capability selection never mutates CA settings or persistent CA Agent definitions.
- Missing selection fields use CA defaults, non-empty arrays are allowlists, and explicit empty arrays allow none.
- Existing Flow `runtime`, `profile_id`, `skill_ids`, and `flow_id` records and existing CA `/api/agent/run` callers remain compatible.
- Public API responses and logs must not expose tokens, model keys, Skill paths/content, or MCP commands/environment.
- Preserve unrelated changes in both dirty repositories.

---

### Task 1: Customer Agent Exact Per-Run Capability Policies

**Files:**
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/core/src/domain/agent/AgentBuilder.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/core/src/domain/agent/AgentBuilder.test.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/shared-run-config.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/shared-run-config.test.ts`

**Interfaces:**
- Consumes: existing `AgentDefinition.capabilities`, `AgentBuilder`, `configureSharedRun`, and `businessCatalog()`.
- Produces: `withExactEnabledTools(names: string[] | null)`, `withExactEnabledSkills(names: string[] | null)`, and optional `SharedRunOptions.enabledTools`, `enabledSkills`, `enabledMCPServers`, and `memoryEnabled`.

- [ ] **Step 1: Inspect the existing builder and run assembly call sites**

Read the four files above and confirm that legacy `withEnabledTools([])` and `withEnabledSkills([])` mean all capabilities, while `AgentConfig` itself can represent an exact empty array.

- [ ] **Step 2: Add exact policy methods without changing legacy semantics**

```ts
withExactEnabledTools(toolNames: string[] | null): this {
  this.enabledTools = toolNames === null ? null : normalizeNames(toolNames);
  return this;
}

withExactEnabledSkills(skillNames: string[] | null): this {
  this.enabledSkills = skillNames === null ? null : normalizeNames(skillNames);
  return this;
}
```

Keep `withEnabledTools([])` and `withEnabledSkills([])` unchanged for saved CA Agent definitions.

- [ ] **Step 3: Extend shared run options and calculate effective policies**

Add optional exact per-run arrays and `memoryEnabled`. When a persistent Agent is selected, intersect requested arrays with its non-empty allowlists. When no persistent Agent is selected, use the requested arrays directly. Omitted fields retain the existing policy.

- [ ] **Step 4: Apply MCP and memory policy during run assembly**

Connect only effective MCP servers. For `memoryEnabled=false`, inject an `IMemoryStore` implementation that returns no context/results and rejects no operations, while session/event persistence continues through `ISessionStore`.

- [ ] **Step 5: Add focused policy tests**

Cover omitted policy, exact empty policy, allowlist intersection, no persistent Agent, MCP filtering, memory disabled, and unchanged legacy empty-array behavior.

### Task 2: Customer Agent Flow Protocol Module and Routes

**Files:**
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/flow-protocol.ts`
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/flow-protocol.test.ts`
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/app/api/flow/v1/catalog/route.ts`
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/app/api/flow/v1/runs/route.ts`
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/app/api/flow/v1/runs/[runId]/events/route.ts`
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/app/api/flow/v1/runs/[runId]/cancel/route.ts`

**Interfaces:**
- Consumes: `agentHost`, `businessCatalog`, `sharedSettings`, `SharedRunOptions`, existing `AgentEvent`, and Bearer-token gateway behavior.
- Produces: `flowCatalog()`, `parseFlowRunRequest(value)`, `registerFlowRun(runId, sessionId)`, `flowRun(runId)`, `forgetFlowRun(runId)`, and `mapFlowEvent(runId, sessionId, id, event)`.

- [ ] **Step 1: Define protocol request, response, and error types**

```ts
export interface FlowRunSelection {
  modelId?: string;
  skillIds?: string[];
  activatedSkillIds?: string[];
  toolIds?: string[];
  mcpServerIds?: string[];
  memoryEnabled?: boolean;
}

export class FlowProtocolError extends Error {
  constructor(readonly code: string, message: string, readonly status = 400) {
    super(message);
  }
}
```

Validate maximum lengths, duplicate IDs, activated-Skill subset rules, supported protocol fields, and capability existence before starting a run.

- [ ] **Step 2: Build the sanitized catalog**

Map CA settings profiles, enabled Skills, runtime Tools, and MCP servers into `{protocolVersion, provider, features, models, skills, tools, mcpServers}`. Return only public identifiers, labels, descriptions, schemas, and availability.

- [ ] **Step 3: Add a bounded in-process run registry**

Store `runId -> {sessionId, createdAt}` for protocol event and cancellation routes. Cap entries and expire terminal/old runs so arbitrary IDs cannot retain memory indefinitely.

- [ ] **Step 4: Implement run admission**

Create or load a CA session, validate optional `agentId`, translate `selection` to exact `SharedRunOptions`, call `agentHost.startRun`, register the run, and return `{runId, sessionId, eventsUrl}` without awaiting completion.

- [ ] **Step 5: Implement normalized SSE mapping and replay**

Map `text_chunk`, `tool_call`, `tool_result`, `done`, `error`, and `turn_aborted` into `assistant.delta`, `tool.started`, `tool.completed`, `run.completed`, and `run.failed`. Emit `run.started` first, preserve monotonically increasing SSE IDs, honor `Last-Event-ID`, and close on a terminal event.

- [ ] **Step 6: Implement cancellation**

Resolve the protocol run, call `agentHost.abort(sessionId)`, and return `{ok: true, runId}`. Return stable `RUN_NOT_FOUND` and `RUN_NOT_ACTIVE` errors where applicable.

- [ ] **Step 7: Add route and protocol tests**

Verify catalog sanitization, request validation, stable error codes, event mapping, replay cursor parsing, completion output, cancellation, and that legacy `/api/agent/run` remains unchanged.

### Task 3: Flow Studio External Provider and Credential Storage

**Files:**
- Create: `src/flow_studio/external_agent.py`
- Create: `tests/test_flow_external_agent.py`
- Modify: `src/flow_studio/workspace.py`

**Interfaces:**
- Consumes: `httpx`, workspace data paths, and CA `/api/flow/v1`.
- Produces: `ExternalAgentError`, `ExternalAgentCredentialStore`, `ExternalAgentProvider`, and `CustomerAgentProvider` with `catalog()`, `run()`, and `cancel()`.

- [ ] **Step 1: Implement the credential store**

Store `{credential_ref: {token, updated_at}}` in a workspace-private JSON file written with mode `0600`. Expose `configured(ref)`, `replace(ref, token)`, `clear(ref)`, and `resolve(ref)`; never return token values from API serializers.

- [ ] **Step 2: Implement provider URL and request validation**

Allow only HTTP/HTTPS URLs without userinfo or fragments. Require a token for non-loopback hosts, disable redirects, set bounded connect/read timeouts, and send the token only in `Authorization`.

- [ ] **Step 3: Implement catalog retrieval**

Fetch `/api/flow/v1/catalog`, require protocol version `1`, validate collection shapes and IDs, and return the normalized catalog unchanged to the server layer.

- [ ] **Step 4: Implement run and SSE consumption**

POST the run request, stream `eventsUrl`, parse SSE `id`, `event`, and multi-line `data`, report tool lifecycle through a callback/logger, return `run.completed.data.text`, and raise `ExternalAgentError` with code/message/remote debug stack on `run.failed`.

- [ ] **Step 5: Add provider tests with a local fake HTTP server**

Cover authentication, catalog parsing, redirects, non-loopback token enforcement, request selection payloads, SSE completion, remote failure, cancellation, malformed events, and token redaction.

### Task 4: Flow Agent Schema, Migration, and Runtime Delegation

**Files:**
- Modify: `src/flow_studio/agentrt.py`
- Modify: `src/flow_studio/engine.py`
- Modify: `tests/test_flow_platform.py`
- Modify: `tests/test_flow_governance.py`

**Interfaces:**
- Consumes: `CustomerAgentProvider`, `ExternalAgentCredentialStore`, existing `AgentStore`, `AgentRuntime.run`, and Flow node log capture.
- Produces: normalized `orchestration` data and external-provider execution through the existing `AgentRuntime.run(...) -> dict` contract.

- [ ] **Step 1: Normalize the three orchestration modes**

Map historical `runtime=customer-agent`, non-empty `flow_id`, and local Agents to explicit orchestration data. Validate provider, URL, credential reference, model, selection arrays, memory flag, and mutual exclusion of mode-specific fields.

- [ ] **Step 2: Preserve compatibility metadata**

Keep list responses usable by historical UI and Flow revisions while returning the new `orchestration` object. Do not rewrite old JSON merely by reading it.

- [ ] **Step 3: Delegate external runs through the provider**

Construct the protocol selection from the Agent, add an explicit node `skill_id` to both allowed and activated Skills, resolve credentials server-side, pass Flow run/node context, and convert provider completion into the existing `{text, steps, session_id, tool_calls, mode}` result.

- [ ] **Step 4: Relay provider lifecycle to node logs**

Log run start, Tool start/completion, provider failure code, and completion duration through the current Python logger so `capture_node_logs` emits `node.log` events. Raise failures so `FlowRunner` retains the complete local traceback.

- [ ] **Step 5: Add migration and runtime tests**

Cover each mode, invalid mixed configuration, legacy records, explicit Skill activation, selected capability payloads, missing credentials, provider errors, cycle guards, and unchanged local ReAct/Flow execution.

### Task 5: Flow Studio Catalog and Credential APIs

**Files:**
- Modify: `src/flow_studio/server.py`
- Modify: `tests/test_flow_server.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: workspace credential store and `CustomerAgentProvider.catalog()`.
- Produces: `POST /api/ai-agents/external/catalog`, `PUT /api/ai-agents/external/credentials/{credential_ref}`, and masked credential status in Agent responses.

- [ ] **Step 1: Add governed request models and endpoints**

The catalog endpoint accepts `{provider, base_url, credential_ref, token?}`. A provided token is used only for that request unless the explicit credential endpoint saves it. The credential endpoint accepts `{token}` to replace and `{clear: true}` to remove.

- [ ] **Step 2: Map provider failures to stable HTTP errors**

Return `{detail, code}` with 400 for invalid configuration, 401 for authentication, 404 for missing provider/catalog IDs, and 502/504 for remote failures/timeouts. Do not include token text or remote secret fields.

- [ ] **Step 3: Add API tests**

Verify governance/CSRF behavior, masked status, save/clear, catalog proxying, non-persistence of one-shot tokens, and sanitized failures.

### Task 6: Three-Mode Agent Editor

**Files:**
- Modify: `src/flow_studio/web/app.js`
- Modify: `src/flow_studio/web/style.css`
- Modify: `tests/test_flow_server.py`

**Interfaces:**
- Consumes: normalized Agent JSON and Flow external catalog/credential APIs.
- Produces: an Agent editor with `ReAct`, `第三方智能体`, and `流程图` controls and saved orchestration configuration.

- [ ] **Step 1: Replace implicit runtime controls with a segmented mode control**

Keep stable dialog dimensions and render only fields for the selected mode. Use text plus existing icon conventions, keyboard focus states, and no nested cards.

- [ ] **Step 2: Build the CA connection and catalog controls**

Show provider, address, masked credential status with replace/clear actions, and a refresh button. Disable refresh while loading and render authentication, network, empty, and unsupported-version errors inline.

- [ ] **Step 3: Build capability selectors**

Render one model select, searchable checkbox lists for Skills/Tools/MCP, and a memory toggle only when supported. Preserve unavailable saved IDs as visibly stale selections until removed.

- [ ] **Step 4: Serialize and validate mode-specific data**

Save only the active mode's configuration, require a model for CA, reject catalog-known invalid IDs, and retain historical values when opening an old Agent.

- [ ] **Step 5: Add static/API-backed UI assertions**

Verify the served script contains the three mode values, catalog endpoint, refresh state, model selector, capability checkboxes, stale-selection handling, and memory toggle.

### Task 7: Integration, Documentation, and Live Verification

**Files:**
- Modify: `src/flow_studio/README.md`
- Modify: `/Users/caoqu/team-agent/customer-agent/README.md`
- Modify: `tests/test_flow_external_agent.py`

**Interfaces:**
- Consumes: completed CA protocol and Flow provider/editor.
- Produces: operator instructions and real local acceptance evidence.

- [ ] **Step 1: Document the protocol and local configuration**

Describe CA URL, optional Bearer token, catalog refresh, selection semantics, memory behavior, and legacy compatibility. Do not document or print an actual token.

- [ ] **Step 2: Run a real local catalog request**

Use Flow Studio's governed endpoint against the running CA instance and verify model, Skill, Tool, and MCP collections contain no secret fields.

- [ ] **Step 3: Run a real CA-backed Agent through Flow**

Select an allowed Skill and bounded Tools/MCP, execute a Flow node, observe live Tool/node events and terminal output, and verify an unselected capability is unavailable.

- [ ] **Step 4: Verify persistence and compatibility**

Reload the Agent editor, confirm selections and masked credential status, confirm no CA Agent definition was created/updated, and execute one existing portfolio Skill path.

- [ ] **Step 5: Verify desktop and mobile rendering**

Use the required browser workflow at desktop and 390x844 viewports. Confirm the modal fits, long names wrap or truncate with titles, controls do not overlap, dragging releases correctly, and all three modes remain selectable.

## Final Unit Test Verification

- [ ] **Main agent: run affected unit tests after development is complete**

Run in Customer Agent:

```bash
bun test packages/core/src/domain/agent/AgentBuilder.test.ts packages/server/lib/shared-run-config.test.ts packages/server/lib/flow-protocol.test.ts
```

Expected: PASS.

Run in Flow Studio:

```bash
pytest -q tests/test_flow_external_agent.py tests/test_flow_platform.py tests/test_flow_server.py tests/test_flow_governance.py
```

Expected: PASS.

If a test fails, fix the implementation or test and rerun the relevant command until it passes. Report both commands and results in the final response.
