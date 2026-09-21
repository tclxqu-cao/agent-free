# Flow Studio Governance Platform Implementation Plan

> **For the main agent:** Implement this plan directly in the current session. Do not dispatch implementation or code-review subagents. After all development tasks are complete, run the affected unit tests and fix any failures before reporting completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local authentication, Workspace tenancy, RBAC, Agent/Flow version approval and rollback, unified policy gates, and audit history to Flow Studio.

**Architecture:** Keep governance metadata and immutable versions in one SQLite database while storing each Workspace's materialized resources under `data/workspaces/<id>/`. A request middleware resolves the authenticated principal and Workspace, then existing APIs use a cached `WorkspaceRuntime`; Agent and Flow writes create versions while formal execution resolves only the published release.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, standard-library `hashlib.scrypt`, JSON file stores, vanilla JavaScript/CSS, pytest/FastAPI TestClient.

## Global Constraints

- No external identity provider, database, or new runtime dependency.
- Existing top-level assets must be copied, not moved, into the `default` Workspace exactly once.
- Passwords, raw Session/CSRF tokens, Cookie values, API keys, and full sensitive prompts must never enter audit details.
- Agent and Flow published snapshots are immutable; rollback creates a new version with lineage.
- All non-public `/api/*` routes require a valid Session and Workspace membership; all unsafe methods also require CSRF.
- Viewer/formal execution reads only published versions; editor preview must name a specific version.

---

### Task 1: Governance Store and Authentication Primitives

**Files:**
- Create: `src/flow_studio/governance.py`
- Unit tests: `tests/test_flow_governance.py`

**Interfaces:**
- Produces: `GovernanceStore(db_path: Path)`, `Principal`, `hash_password()`, `verify_password()`, role/capability constants, Session and audit methods.
- Consumes: only Python standard library and SQLite.

- [ ] **Step 1: Add the SQLite schema and deterministic row helpers**

Create tables for `users`, `workspaces`, `members`, `sessions`, `login_attempts`, `policies`, `resource_versions`, `resource_releases`, `audit_events`, and `migrations`. Enable WAL, foreign keys, row factory, and an `RLock` around write transactions.

```python
@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    workspace_id: str
    role: str
    session_id: str
    csrf_token: str
    request_id: str

ROLE_CAPABILITIES = {
    "viewer": {"resource.read", "runtime.execute"},
    "editor": {"resource.read", "runtime.execute", "resource.write",
               "runtime.preview", "eval.execute"},
    "admin": {"resource.read", "runtime.execute", "resource.write",
              "runtime.preview", "eval.execute", "release.approve",
              "release.publish", "release.rollback", "member.manage",
              "audit.read"},
    "owner": {"*"},
}
```

- [ ] **Step 2: Implement password, setup, login throttling, Session, CSRF, Workspace, membership, policy, version, release, and audit operations**

Use `hashlib.scrypt(password, salt=salt, n=2**14, r=8, p=1)`. Store only a versioned encoded hash. Session tokens use `secrets.token_urlsafe(32)` and database SHA-256 digests. Implement transaction-safe methods with explicit validation and stable `GovernanceError(code, message, status)` failures.

- [ ] **Step 3: Add focused unit tests**

Cover hash verification, setup idempotency, five-failure lockout, Session expiry and revoke, CSRF equality, role matrix, owner/admin member limits, Workspace membership isolation, append-only audit redaction, and migration markers.

### Task 2: Workspace Runtime and Legacy Migration

**Files:**
- Create: `src/flow_studio/workspace.py`
- Modify: `src/flow_studio/server.py`
- Unit tests: `tests/test_flow_governance.py`

**Interfaces:**
- Consumes: existing stores and `GovernanceStore`.
- Produces: `WorkspaceRuntime`, `WorkspaceManager.get(workspace_id)`, `migrate_legacy_default()`, and CLI `--data-dir` isolation support.

- [ ] **Step 1: Extract Workspace-scoped assembly from `Studio`**

```python
class WorkspaceRuntime:
    def __init__(self, root: Path, config: dict, registry: AgentRegistry,
                 observer, version_service=None): ...

class WorkspaceManager:
    def get(self, workspace_id: str) -> WorkspaceRuntime: ...
    def invalidate(self, workspace_id: str) -> None: ...
```

Each runtime constructs Flow, Run, Agent, knowledge, memory, skill, MCP, evaluation, media and video stores under the Workspace root. Shared instance config, registry and observer remain instance-scoped.

- [ ] **Step 2: Implement idempotent default migration**

Copy `flows`, `ai_agents`, `kb`, `skills`, `evals`, `media`, `memory.sqlite`, `mcp.json`, and `video_models.json` when the destination item does not exist. Register copied Agent and Flow files as published v1, then write migration key `legacy_to_default_v1` only after all registration succeeds.

- [ ] **Step 3: Test isolation and migration**

Assert two runtimes can use the same resource ID with different content, legacy source remains unchanged, published v1 is registered once, and a second startup does not create versions or copy again.

### Task 3: Policy Engine and Version Service

**Files:**
- Create: `src/flow_studio/policy.py`
- Create: `src/flow_studio/versioning.py`
- Modify: `src/flow_studio/evals.py`
- Unit tests: `tests/test_flow_governance.py`

**Interfaces:**
- Produces: `PolicyEngine.evaluate(stage, resource_type, snapshot, context) -> PolicyResult` and `VersionService` save/submit/approve/reject/publish/rollback/preview resolution.
- Consumes: `GovernanceStore`, `WorkspaceManager`, existing graph/agent serializers and EvalStore.

- [ ] **Step 1: Implement normalized policy validation**

```python
DEFAULT_POLICY = {
    "allowed_models": [], "denied_tools": [], "denied_mcp_servers": [],
    "allowed_flow_node_types": [], "max_agent_steps": 12,
    "allowed_http_hosts": [], "require_approval": True,
    "required_eval_suite_id": "", "min_eval_pass_rate": 1.0,
}
```

Check Agent model/tool/MCP/max-step fields and Flow node types plus HTTP URL hosts. Return stable codes such as `tool_denied`, `mcp_denied`, `node_type_denied`, `http_host_denied`, `max_steps_exceeded`, `approval_required`, and `evaluation_required`.

- [ ] **Step 2: Implement version state transitions and materialization**

`save_draft()` deduplicates by content hash; `submit()` runs submit policy; `approve()` enforces author separation with the sole-approver owner exception; `publish()` checks approval/current policy/evaluation and atomically replaces materialized JSON; `rollback()` creates and publishes a new version with `rollback_of`; deletion versions remove materialized files on publish.

- [ ] **Step 3: Extend evaluation target execution to support Flow targets**

Add optional `flow_runner(flow_id, question)` to `TargetRunner`; target type `flow` returns its output and node steps. Add `EvalStore.latest_pass_rate(suite_id, target_key)` for publish policy evidence.

- [ ] **Step 4: Test policy and lifecycle behavior**

Cover every policy code at submit and run, author approval denial, sole-owner self-approval, idempotent repeated transition, delete publication, rollback lineage, materialization failure compensation, and Agent/Flow evaluation pass-rate checks.

### Task 4: Authentication, Workspace, RBAC, Governance APIs

**Files:**
- Modify: `src/flow_studio/server.py`
- Unit tests: `tests/test_flow_governance.py`

**Interfaces:**
- Consumes: `GovernanceStore`, `WorkspaceManager`, `VersionService`, `PolicyEngine`.
- Produces: setup/auth/me/workspace/member/policy/audit/approval/version endpoints and request state.

- [ ] **Step 1: Add request middleware**

Public routes are static files, health, setup status, setup and login. Every other `/api/*` request resolves Session, requires CSRF on POST/PUT/PATCH/DELETE, verifies `X-Workspace-ID` membership and attaches Principal plus WorkspaceRuntime. Return stable JSON errors with `code` and `request_id`.

- [ ] **Step 2: Add setup and local account endpoints**

Implement setup, login, logout and `/api/me`; set/clear the Session Cookie with secure attributes. Login responses include CSRF and accessible Workspaces but never token material.

- [ ] **Step 3: Add Workspace/member and governance endpoints**

Implement the exact routes in the design spec. Enforce capabilities through one `require_capability(request, name)` helper. Sanitize all audit details before append.

- [ ] **Step 4: Add API integration tests**

Use separate TestClient sessions for owner/admin/editor/viewer. Prove 401, CSRF 403, role 403, owner/admin member constraints, tenant selection, policy CRUD, approval queues, audit filtering and stable error bodies.

### Task 5: Govern Existing Asset and Runtime APIs

**Files:**
- Modify: `src/flow_studio/server.py`
- Modify: `src/flow_studio/agentrt.py`
- Modify: `src/flow_studio/engine.py`
- Unit tests: `tests/test_flow_governance.py`
- Unit tests: `tests/test_flow_server.py`
- Unit tests: `tests/test_flow_platform.py`

**Interfaces:**
- Consumes: request Principal, WorkspaceRuntime, VersionService and PolicyEngine.
- Produces: Workspace-scoped existing APIs with draft writes and published-only execution.

- [ ] **Step 1: Route every existing API through the request WorkspaceRuntime**

Replace global `studio.flows`, `studio.kb`, `studio.memory`, `studio.skills`, `studio.mcp`, `studio.evals`, `studio.media`, `studio.vmodels`, `studio.ai_agents`, and runtime usage in request handlers. Instance-level node metadata and Job Agent action registry stay shared.

- [ ] **Step 2: Change Agent and Flow writes into version drafts**

POST/PUT creates an immutable upsert draft; DELETE creates a delete draft. Editor-facing GET/list overlays the latest visible version and adds `_governance: {version, status, published_version}` metadata. Viewer reads only published materialization.

- [ ] **Step 3: Gate all execution paths**

Flow run, Agent invoke, chat routing and function-tool flow invocation resolve published snapshots only, call run policy immediately before execution, and write audit outcome. Governance preview explicitly resolves the named version and marks runs `preview=true`.

- [ ] **Step 4: Update existing tests for authenticated setup**

Extend fixtures to initialize and log in an owner, attach CSRF automatically, and preserve existing behavioral assertions. Add one anonymous client fixture for auth-negative tests.

### Task 6: Web Authentication, Workspace and Governance UI

**Files:**
- Modify: `src/flow_studio/web/index.html`
- Modify: `src/flow_studio/web/app.js`
- Modify: `src/flow_studio/web/style.css`
- Modify: `src/flow_studio/README.md`

**Interfaces:**
- Consumes: auth, Workspace and governance APIs.
- Produces: complete setup/login shell, Workspace selector, role-aware controls and governance center.

- [ ] **Step 1: Add setup/login application states and authenticated API wrapper**

Bootstrap with `/api/setup/status`, then `/api/me`. Store CSRF and active Workspace only in JavaScript memory/localStorage respectively; every unsafe request adds `X-CSRF-Token`, every governed request adds `X-Workspace-ID`. On 401 render login, on 403 show permission error, on 409 refresh affected data.

- [ ] **Step 2: Add global Workspace/user controls**

Add a compact Workspace selector, role badge, username and logout icon to the existing header. Switching clears cached resource state and reloads the selected Workspace.

- [ ] **Step 3: Add resource version actions**

Agent cards and Flow toolbar show version/status. Save remains draft-only; render submit for editor+, approve/reject/publish/rollback for authorized roles. Each action opens a concise reason dialog and refreshes resource plus approval state.

- [ ] **Step 4: Add governance center**

Add unframed tabs for Overview, Approvals, Versions, Members, Policy and Audit. Use existing buttons, fields, chips and compact rows; hide unauthorized actions based on `/api/me` capabilities while retaining server enforcement.

- [ ] **Step 5: Document setup and security boundary**

Update README with first-owner setup, role matrix, Workspace behavior, version lifecycle, policy fields, backup files and the warning that non-loopback deployment still needs TLS/reverse-proxy hardening.

### Task 7: Full Regression and Browser Acceptance

**Files:**
- Modify as failures require: files from Tasks 1–6 only.

**Interfaces:**
- Consumes: completed application.
- Produces: verified backend and rendered workflows.

- [ ] **Step 1: Run focused governance and Flow Studio tests**

```bash
uv run pytest tests/test_flow_governance.py tests/test_flow_server.py \
  tests/test_flow_platform.py tests/test_flow_engine.py tests/test_flow_store.py -v
```

- [ ] **Step 2: Run the full test suite**

```bash
uv run pytest tests/ -v
```

- [ ] **Step 3: Start an isolated acceptance instance**

```bash
accept_root=$(mktemp -d /tmp/flow-studio-governance.XXXXXX)
mkdir -p "$accept_root/config" "$accept_root/data"
lsof -nP -iTCP:8790 -sTCP:LISTEN
uv run flow-studio --config-dir "$accept_root/config" --data-dir "$accept_root/data" --port 8790 --no-open
```

Use a temporary data directory so acceptance does not mutate checked-in or personal data.

- [ ] **Step 4: Verify browser workflows on desktop and mobile**

Use the required `$ego-browser` workflow to validate owner setup/login, Workspace switch, editor draft/submit, admin approval/publish, viewer execution/action hiding, rollback, policy denial and audit visibility. Capture rendered evidence and check for overlap at desktop and mobile widths.

- [ ] **Step 5: Inspect final diff and repository state**

Run `git diff --check`, review only task-owned files, confirm no credentials or runtime data were added, and report the exact test and browser acceptance boundary.

## Final Unit Test Verification

- [ ] **Main agent: run affected unit tests after development is complete**

Run: `uv run pytest tests/test_flow_governance.py tests/test_flow_server.py tests/test_flow_platform.py tests/test_flow_engine.py tests/test_flow_store.py -v`

Expected: PASS

If a test fails, fix the implementation or test and rerun this command until it passes. Report the command and result in the final response.
