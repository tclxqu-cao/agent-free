# CA Dynamic Agent Orchestration Implementation Plan

> **For the main agent:** Implement this plan directly in the current session. Do not dispatch implementation or code-review subagents. After all development tasks are complete, run the affected unit tests and fix any failures before reporting completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one Flow-configured Customer Agent dynamically create and coordinate first-level temporary child Agents while Flow persists, visualizes, and replays the live runtime team without changing manual Workflow behavior.

**Architecture:** Customer Agent owns dynamic planning, ephemeral child definitions, capability narrowing, limits, cancellation, and parent/child events. Flow Studio sends only published orchestration constraints, normalizes the new SSE events into its existing Run event log, stores a `dynamicTeamSnapshot` on the owning node run, and derives temporary canvas/stage nodes from that runtime state without mutating the saved graph.

**Tech Stack:** TypeScript, Zod, Vitest, Next.js route handlers, Python 3.11, httpx SSE, pytest, native browser JavaScript, CSS.

## Global Constraints

- Keep the existing `react / external_agent / flow` Agent modes; dynamic teams are an optional `external_agent.dynamic_team` capability.
- A child Agent must never receive `spawn_agent`, `dispatch_agent`, or `wait_agent`; maximum spawn depth is exactly `1`.
- The model may provide only `name`, `role`, `task`, and `instructions`; model, Skills, Tools, MCP servers, memory, and policy are inherited and narrowed by the server.
- Default limits are `max_workers=6`, `max_parallel=3`, and `worker_timeout_seconds=900`; accepted bounds are workers `1..12`, parallelism `1..6` and no greater than workers, timeout `30..3600` seconds.
- Saved Flow graphs remain static; temporary children are persisted only in run events and `dynamicTeamSnapshot`.
- Preserve all pre-existing uncommitted Customer Agent changes and use Node 22 for Customer Agent verification.

---

### Task 1: Customer Agent Flow Protocol Contract

**Files:**
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/flow-protocol.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/flow-protocol.test.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/shared-run-config.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/app/api/flow/v1/runs/route.ts`

**Interfaces:**
- Consumes: existing `FlowRunRequest`, `SharedRunOptions`, and `AgentEvent` contracts.
- Produces: `FlowDynamicTeamOrchestration`, `FlowRunRequest.orchestration`, `SharedRunOptions.dynamicTeam`, and normalized `agent.spawned|started|progress|completed|failed` events.

- [x] **Step 1: Extend and validate the Flow request**

Add exact bounded parsing for the optional request shape:

```ts
export interface FlowDynamicTeamOrchestration {
  mode: "dynamic_team";
  maxWorkers: number;
  maxParallel: number;
  workerTimeoutSeconds: number;
}

export interface FlowRunRequest {
  input: string;
  instructions?: string;
  agentId?: string;
  sessionId?: string;
  context: Record<string, unknown>;
  selection: FlowRunSelection;
  orchestration?: FlowDynamicTeamOrchestration;
}
```

Reject unsupported modes, non-integers, out-of-range values, and `maxParallel > maxWorkers` with `FlowProtocolError("INVALID_REQUEST", ...)`.

- [x] **Step 2: Advertise and pass the trusted capability**

Return these catalog feature values and forward the parsed object through the authenticated route only:

```ts
features: {
  streaming: true,
  memory: true,
  cancellation: true,
  dynamicAgentOrchestration: true,
  maxSpawnDepth: 1,
}

dynamicTeam: body.orchestration
```

The browser cannot add or override this field because Flow Studio constructs the server-to-server request from the saved Agent definition.

- [x] **Step 3: Publish dynamic child events**

Extend `NormalizedFlowEvent["event"]` and `mapFlowEvent()` so `agent_dispatch` becomes `agent.spawned`, the first progress for a child becomes `agent.started`, later progress becomes `agent.progress`, and `agent_done` becomes `agent.completed` or `agent.failed`. Include `agentId`, `sessionId`, `parentSessionId`, name, role, task, progress fields, terminal detail, and the existing parent cursor `id`.

- [x] **Step 4: Add focused protocol tests**

Cover accepted defaults, every limit rejection, catalog capability fields, and all five normalized event types while retaining the existing assistant/tool/run assertions.

### Task 2: Customer Agent Ephemeral Spawn Runtime

**Files:**
- Create: `/Users/caoqu/team-agent/customer-agent/packages/core/src/domain/tool/builtin/SpawnAgentTool.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/core/src/domain/tool/builtin/index.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/core/src/index.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/core/src/domain/agent/entities.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/core/src/domain/tool/permissions.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/native-runtime/src/sub-agent-dispatcher.ts`
- Create: `/Users/caoqu/team-agent/customer-agent/packages/native-runtime/src/sub-agent-dispatcher.test.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/runtime-tool-catalog.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/app/api/agent-host.ts`

**Interfaces:**
- Consumes: `SharedRunOptions.dynamicTeam`, the parent run capability snapshot, `AgentDefinition`, and the existing `SubAgentDispatcher` lifecycle.
- Produces: `SpawnAgentTool`, `DynamicTeamLimits`, `SubAgentDispatcher.spawn()`, `SubAgentDispatcher.waitForIdle()`, and richer child lifecycle events.

- [x] **Step 1: Define the bounded tool and event payloads**

Implement a Zod-backed tool whose callback is:

```ts
export interface SpawnAgentInput {
  name: string;
  role: string;
  task: string;
  instructions?: string;
}

export class SpawnAgentTool implements ITool {
  constructor(
    private readonly spawnFn: (
      input: SpawnAgentInput,
      parentSessionId: string,
    ) => Promise<DispatchResult>,
  ) {}
}
```

Bound name to 80 characters, role to 500, task to 8,000, and instructions to 8,000. Add optional `agentId`, `role`, `startedAt`, and `durationMs` fields to the existing Agent events without breaking persisted legacy events.

- [x] **Step 2: Add run-scoped limits and capability inheritance**

Add:

```ts
export interface DynamicTeamLimits {
  maxWorkers: number;
  maxParallel: number;
  workerTimeoutMs: number;
  runId: string;
  model: NonNullable<SharedRunOptions["model"]>;
  enabledTools?: string[];
  enabledSkills?: string[];
  enabledMCPServers?: string[];
  memoryEnabled?: boolean;
  toolExecutionPolicy?: ToolExecutionPolicy;
}
```

Create ephemeral `AgentDefinition` values in memory only, never call `agentStore.create/update`, use the resolved parent model, intersect tool and Skill allowlists, and remove all coordination tools before building a child.

- [x] **Step 3: Enforce lifecycle and convergence**

Track total spawned children and active children per dispatcher. Reject the next spawn with structured `MAX_WORKERS_EXCEEDED` or `MAX_PARALLEL_EXCEEDED` errors, apply a per-child abort timeout, abort every child with the parent, retain completed results for `wait_agent`, and implement:

```ts
async waitForIdle(timeoutMs: number): Promise<DispatchResult[]>;
```

Call `waitForIdle()` before a parent run is committed as successful; timed-out children become failed events and mailbox messages.

- [x] **Step 4: Register tools at the correct depth**

Register `spawn_agent` and `wait_agent` only when the authenticated Flow request enables a dynamic team. Keep existing persisted-Agent `dispatch_agent` behavior for ordinary parent runs. Build child Agents after registering their non-coordination session tools and never register spawn/dispatch/wait for a child session.

- [x] **Step 5: Add dispatcher tests**

Use in-memory/fake stores and a fake Agent loop to prove ephemeral definitions are not saved, limits and timeouts work, child tools are narrowed, events retain the parent session cursor path, results remain waitable after completion, and `abortAll()` reaches every active child.

### Task 3: Flow Studio Agent Configuration and Provider Events

**Files:**
- Modify: `/Users/caoqu/agent-free/src/flow_studio/agentrt.py`
- Modify: `/Users/caoqu/agent-free/src/flow_studio/external_agent.py`
- Modify: `/Users/caoqu/agent-free/tests/test_flow_external_agent.py`
- Modify: `/Users/caoqu/agent-free/tests/test_flow_platform.py`

**Interfaces:**
- Consumes: CA catalog feature flags and Flow v1 dynamic event names.
- Produces: normalized `orchestration.dynamic_team`, `CustomerAgentProvider.run(..., orchestration=...)`, and returned `dynamic_team_snapshot` plus `dynamic_team_events`.

- [x] **Step 1: Normalize saved dynamic-team settings**

Implement a helper that returns `None` when disabled and otherwise returns:

```python
{
    "enabled": True,
    "max_workers": max_workers,
    "max_parallel": max_parallel,
    "worker_timeout_seconds": worker_timeout_seconds,
}
```

Apply the exact bounds from Global Constraints. Reject enabled settings unless the freshly supplied CA catalog reports `features.dynamicAgentOrchestration is True` at save time.

- [x] **Step 2: Send the orchestration request**

Extend the provider signature with `orchestration: dict | None = None` and serialize enabled settings as:

```python
body["orchestration"] = {
    "mode": "dynamic_team",
    "maxWorkers": config["max_workers"],
    "maxParallel": config["max_parallel"],
    "workerTimeoutSeconds": config["worker_timeout_seconds"],
}
```

- [x] **Step 3: Normalize live team state**

For each `agent.*` SSE event, forward the original event to the execution observer and update a dictionary keyed by `agentId`. Return both the ordered event list and:

```python
{
    "supervisorSessionId": resolved_session,
    "agents": [{"agentId": "...", "sessionId": "...", "status": "completed"}],
}
```

Validate required strings, cap event text stored in the run, and treat unknown events as forward-compatible no-ops.

- [x] **Step 4: Add provider and normalization tests**

Assert the exact request body, catalog gating, event ordering, same-agent state updates, failed terminal state, and unchanged single-Agent behavior when the option is absent.

### Task 4: Flow Run Persistence and Dynamic-Team Snapshot

**Files:**
- Modify: `/Users/caoqu/agent-free/src/flow_studio/agentrt.py`
- Modify: `/Users/caoqu/agent-free/src/flow_studio/engine.py`
- Modify: `/Users/caoqu/agent-free/tests/test_flow_engine.py`
- Modify: `/Users/caoqu/agent-free/tests/test_flow_live_runs.py`

**Interfaces:**
- Consumes: Agent runtime `event_sink(event_type, event)` and `dynamic_team_snapshot`.
- Produces: `agent.spawned|started|progress|completed|failed` records with the owning `node_id`, and `NodeRun.output.dynamicTeamSnapshot`.

- [x] **Step 1: Thread an event observer through AgentRuntime**

Extend the runtime signature without changing existing callers:

```python
def run(self, agent: dict, message: str, session_id: str | None = None,
        flow_run_id: str | None = None, skill_id: str | None = None,
        context: dict | None = None,
        event_sink: Callable[[str, dict], None] | None = None) -> dict:
```

Invoke it only with normalized, JSON-safe CA events.

- [x] **Step 2: Persist events against the owning Flow node**

In `_run_ai_agent`, map the observer into the current `_active_node_run` and call `_emit_external_agent_event()` so the existing `RunStore.record(snapshot, event)` transaction persists the latest snapshot and event together. Set `node_id` and `node_label` from the owning static CA node and keep `agentId` and child session IDs as payload fields.

- [x] **Step 3: Persist the final team snapshot**

Return `dynamicTeamSnapshot` in the node output and retain it after partial/failed CA execution. Do not append children to `RunResult.graph.nodes`, and do not modify the saved Flow definition.

- [x] **Step 4: Add run persistence tests**

Run a fake external provider that emits two children and assert events are cursor ordered, the snapshot rebuilds both terminal states, refresh reads the same run, and two CA nodes with identical child names remain isolated by `node_id` and `agentId`.

### Task 5: Runtime Canvas and Digital-Human Projection

**Files:**
- Modify: `/Users/caoqu/agent-free/src/flow_studio/web/app.js`
- Modify: `/Users/caoqu/agent-free/src/flow_studio/web/style.css`
- Modify: `/Users/caoqu/agent-free/tests/test_flow_server.py`

**Interfaces:**
- Consumes: persisted dynamic Agent events and `dynamicTeamSnapshot` from `Run.node_runs[*].output`.
- Produces: `dynamicTeamProjection(run, events)`, temporary editor-canvas nodes/edges, snapshot-modal nodes/edges, and digital-human avatars driven by the same projection.

- [x] **Step 1: Derive one replayable projection**

Implement a pure function:

```js
function dynamicTeamProjection(run, events = []) {
  return { nodes: [], edges: [], byRuntimeId: new Map() };
}
```

Use IDs in the form `runtime:${flowNodeId}:${agentId}`. Seed from every node output snapshot, then apply ordered `agent.*` events so live polling and historical replay converge to the same topology and status.

- [x] **Step 2: Render temporary nodes on the editor canvas**

Maintain separate runtime node/edge maps so graph editing, saving, selection, minimap, and graph equality continue to see only `state.graph`. Position children in bounded rows to the right of their owning CA node, use a dashed outline and `临时` badge, and open a read-only detail modal showing role, task, progress, tool, duration, child session, summary, and failure.

- [x] **Step 3: Include children in run snapshot and node list**

Merge the derived nodes and edges only for display in `refreshRunModal()` and add child rows beneath the owning CA node in `renderRunPanel()`. Never write the merged value back to `run.graph`.

- [x] **Step 4: Reuse the projection on the digital-human stage**

Build the stage from static nodes plus projected children, place each supervisor before its children, draw supervisor-to-child edges, add avatars when `agent.spawned` arrives, and apply queued/running/completed/failed/cancelled state and progress bubbles from the same event stream.

- [x] **Step 5: Style and contract-test the UI**

Add dashed temporary-node styles, fixed label widths, overflow handling, and status colors for both themes and mobile. Assert the served JavaScript contains the projection/event handlers and the saved graph endpoint still returns no runtime nodes.

### Task 6: Live Integration Acceptance

**Files:**
- Modify only when a failing acceptance check reveals a defect in a file already listed above.

**Interfaces:**
- Consumes: completed CA and Flow implementations.
- Produces: live evidence for catalog, request validation, event streaming, persistence, canvas projection, and digital-human replay.

- [x] **Step 1: Build and start isolated local services**

Use Node 22 for Customer Agent and start Flow Studio on free local ports. Do not stop or replace unrelated user-owned processes.

- [x] **Step 2: Verify the protocol without a model call**

Read the live CA catalog and submit invalid limit combinations to prove capability discovery and server-side validation. Confirm legacy requests without `orchestration` still admit normally only when a disposable session is used.

- [x] **Step 3: Exercise a dynamic run**

Use a configured CA model to request at least two distinct temporary roles, observe all event classes, parent convergence, child sessions, and absence of new persistent Agent assets. If the configured model or credential is unavailable, report this live-model acceptance as blocked while retaining deterministic test evidence.

- [x] **Step 4: Verify the UI with ego-browser**

Open Flow Studio through the `ego-browser` skill, enable dynamic team settings, run or load the dynamic run, and verify desktop and mobile layouts for the editor overlay, run detail, digital-human stage, refresh, and historical replay.

## Final Unit Test Verification

- [x] **Main agent: run affected unit tests after development is complete**

Run from `/Users/caoqu/team-agent/customer-agent` with Node 22:

```bash
bunx vitest run packages/server/lib/flow-protocol.test.ts packages/native-runtime/src/sub-agent-dispatcher.test.ts packages/server/app/api/agent-host.test.ts
bunx tsc --noEmit
```

Run from `/Users/caoqu/agent-free`:

```bash
uv run pytest tests/test_flow_external_agent.py tests/test_flow_platform.py tests/test_flow_engine.py tests/test_flow_live_runs.py tests/test_flow_server.py -v
```

Expected: PASS

Verified on 2026-09-27: the focused Customer Agent suite passed 63 tests, the
Flow Studio suite passed 104 tests, and the Core plus Native Runtime package
builds completed. The repository-wide root TypeScript check still includes
pre-existing `outputs/` previews and unrelated test fixtures with known type
errors; package-scoped declaration builds for the changed runtime passed.

If a test fails, fix the implementation or test and rerun this command until it passes. Report the command and result in the final response.
