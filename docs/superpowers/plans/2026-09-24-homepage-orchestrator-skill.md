# Homepage Orchestrator Skill Implementation Plan

> **For the main agent:** Implement this plan directly in the current session. Do not dispatch implementation or code-review subagents. After all development tasks are complete, run the affected unit tests and fix any failures before reporting completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every homepage request through one prompt-producing decision node and one configured Customer Agent whose orchestration Skill loads query, polish, and render Skills.

**Architecture:** Flow Studio adds an optional node-local output mode to its existing condition node while preserving edge-routing compatibility. The published `homepage-main` graph becomes `start -> prompt_decision -> homepage_agent -> end`; Customer Agent discovers four project Skills and the Agent activates only `homepage-orchestrator`, which loads the other stages during the same AgentLoop.

**Tech Stack:** Python 3 Flow Studio runtime and tests, vanilla JavaScript Flow editor, TypeScript Customer Agent server, Markdown SKILL definitions, Vitest, Node.js 22.

## Global Constraints

- Preserve all unrelated dirty-worktree changes in both repositories.
- Keep `public_wiki_query` backward compatible; do not use it as the new orchestration mechanism.
- Permit read-only retrieval from the complete `~/.obsidian/wiki` only through the configured `wiki-query` Skill and its required tools.
- Public `/jobs` requests may read only the last successful job snapshot and may never scrape or apply.
- Reuse one session until page refresh; a refresh creates a new session.
- Do not launch an isolated service; restart the existing Flow Studio, Customer Agent, and homepage services after tests.

---

### Task 1: Condition Node Output Contract

**Files:**
- Modify: `src/flow_studio/graph.py`
- Modify: `src/flow_studio/engine.py`
- Modify: `src/flow_studio/web/app.js`
- Unit tests: `tests/test_flow_graph.py`
- Unit tests: `tests/test_flow_engine.py`

**Interfaces:**
- Consumes: condition params `source`, ordered `outputs`, and `default_output`; Flow template context.
- Produces: condition output `{"matched": bool, "rule": str, "text": str}` and one ordinary downstream route in output mode.

- [x] **Step 1: Inspect the existing condition validation, execution, editor, and branch tests**

Read the condition-specific functions and confirm legacy edge expressions remain valid when `params.outputs` is absent.

- [x] **Step 2: Validate output-mode configuration**

Require every output rule to contain a non-empty `name`, `expression`, and string `output`, reject duplicate names, and require a string `default_output`. Permit one ordinary outgoing edge in this mode.

- [x] **Step 3: Execute output-mode rules in order**

Evaluate each expression with the existing restricted expression evaluator, render the selected output template, and fall back to `default_output`; preserve the existing empty result and edge selection for legacy conditions.

- [x] **Step 4: Expose output rules in the Flow editor**

Add repeatable fields for rule name, expression, output template, and default output. Store them under the condition node params without moving expressions onto edges.

- [x] **Step 5: Add compatibility and output tests**

Cover first-match ordering, template rendering, default output, one-edge traversal, invalid configuration, and unchanged legacy branch routing.

### Task 2: Homepage Agent Graph and Runtime

**Files:**
- Modify: `src/flow_studio/default_agents.json`
- Modify: `src/flow_studio/homepage_flows.py`
- Modify: `src/flow_studio/homepage.py`
- Modify: `src/flow_studio/agentrt.py`
- Unit tests: `tests/test_homepage_flow.py`
- Unit tests: `tests/test_flow_builtin.py`
- Unit tests: `tests/test_flow_platform.py`

**Interfaces:**
- Consumes: original homepage `message` and page-scoped `session_id`.
- Produces: one `homepage-agent` invocation with activated Skill `homepage-orchestrator` and a validated `PortfolioArtifactV1` response.

- [x] **Step 1: Replace command branches with the four-node homepage graph**

Configure ordered command expressions and task-prompt outputs for `/help`, `/whoami`, `/works`, `/project <name>`, `/timeline`, `/contact`, `/jobs`, `/job`, unknown slash commands, and natural-language default input.

- [x] **Step 2: Define one homepage Agent**

Enable `homepage-orchestrator`, `wiki-query`, `homepage-content-polish`, and `homepage-content-render` plus the minimum read-only tools required by those Skills. Activate only `homepage-orchestrator` at the Flow node.

- [x] **Step 3: Remove server-side Skill selection**

Send the original message and session ID to `homepage-main`; validate the returned artifact and cache only its canonical exact-command kind. Never use unrelated cache entries for natural language.

- [x] **Step 4: Generalize artifact parsing**

Parse and validate artifact output for the homepage orchestrator even though the activated Skill name does not start with `portfolio-`.

- [x] **Step 5: Update homepage contract tests**

Assert the exact four-node topology, decision output prompts, a single Agent path for commands and natural language, session reuse, exact-command fallback isolation, and read-only jobs behavior.

### Task 3: Customer Agent Skill Pipeline

**Files:**
- Create: `/Users/caoqu/team-agent/customer-agent/.agent/skills/wiki-query/SKILL.md`
- Create: `/Users/caoqu/team-agent/customer-agent/.agent/skills/homepage-orchestrator/SKILL.md`
- Create: `/Users/caoqu/team-agent/customer-agent/.agent/skills/homepage-content-polish/SKILL.md`
- Create: `/Users/caoqu/team-agent/customer-agent/.agent/skills/homepage-content-render/SKILL.md`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/portfolio-skill-catalog.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/shared-run-config.ts`
- Unit tests: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/portfolio-content-agent.test.ts`

**Interfaces:**
- Consumes: exact enabled Skill IDs and one activated `homepage-orchestrator` Skill.
- Produces: file-backed Skill definitions available to `skill_load`, with the orchestrator enforcing `wiki-query -> homepage-content-polish -> homepage-content-render` for evidence-backed requests.

- [x] **Step 1: Add the read-only Wiki query Skill**

Scope reads to `~/.obsidian/wiki`, require bounded evidence and vault-relative sources, reject writes, and treat retrieved instructions as untrusted content.

- [x] **Step 2: Add polish and render Skills**

Define structured semantic content and final `PortfolioArtifactV1` contracts, including allowed block types, safe media paths, shared HTML classes, and source preservation.

- [x] **Step 3: Add the orchestrator Skill**

Require exact `skill_load` calls for the needed stages, allow direct chat only when no stored facts are needed, and require snapshot-only handling for jobs.

- [x] **Step 4: Generalize project Skill discovery**

Load approved `portfolio-*`, `homepage-*`, and `wiki-query` definitions from the project Skill directory whenever any selected Skill is file-backed.

- [x] **Step 5: Add discovery tests**

Verify all four new Skills load with their prompts and that unrelated project Skills remain excluded from the Flow catalog.

### Task 4: Integrated Verification and Existing-Service Restart

**Files:**
- Modify only when a failing focused test reveals a contract mismatch in an already listed file.

**Interfaces:**
- Consumes: completed Flow and Customer Agent changes.
- Produces: passing focused tests, successful builds, stable existing services, and browser-visible homepage evidence.

- [x] **Step 1: Run focused Python tests**

Run `pytest tests/test_flow_engine.py tests/test_flow_graph.py tests/test_homepage_flow.py tests/test_flow_builtin.py tests/test_flow_platform.py -q` and fix failures.

- [x] **Step 2: Run focused Customer Agent tests and builds with Node.js 22**

Run the relevant Vitest files plus Core, SDK, and Server build commands defined by the repository scripts; fix failures.

- [x] **Step 3: Inspect active runs and restart existing services**

Check current Flow and Customer Agent runs before restart, then use the existing service ownership and launch configuration for ports `3000`, `8788`, and `8801`.

- [x] **Step 4: Verify HTTP and browser behavior**

Confirm stable health endpoints and exercise `/contact`, `/project agentroam`, natural-language chat, and `/jobs`; verify four-node run progress, artifact media rendering, and same-page session reuse with `ego-browser`.

- [x] **Step 5: Capture reusable findings**

Use `wiki-capture` to record the final architecture, compatibility boundary, implementation gotchas, and runtime verification evidence.

## Final Unit Test Verification

- [x] **Main agent: run affected unit tests after development is complete**

Run: `pytest tests/test_flow_engine.py tests/test_flow_graph.py tests/test_homepage_flow.py tests/test_flow_builtin.py tests/test_flow_platform.py -q`

Run: `bun test packages/server/lib/portfolio-content-agent.test.ts packages/server/lib/flow-protocol.test.ts`

Expected: PASS

If a test fails, fix the implementation or test and rerun these commands until they pass. Report the commands and results in the final response.
