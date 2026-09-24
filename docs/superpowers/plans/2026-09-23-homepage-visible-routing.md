# Homepage Visible Routing Implementation Plan

> **For the main agent:** Implement this plan directly in the current session. Do not dispatch implementation or code-review subagents. After all development tasks are complete, run the affected unit tests and fix any failures before reporting completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hidden homepage route action with visible Slash-condition and natural-language intent branches, and add safe structured value comparisons to Flow Studio condition nodes.

**Architecture:** Condition nodes resolve one configured namespace path and evaluate typed comparison metadata stored on each outgoing edge, while legacy raw expressions remain supported. `homepage-main` sends exact Slash commands from the condition node to fixed Agent Skill nodes and sends only the fallback path to an intent node that directly selects the same fixed branches.

**Tech Stack:** Python 3.11, FastAPI, pytest, vanilla JavaScript, Flow Studio JSON graphs.

## Global Constraints

- Preserve all existing dirty-worktree changes.
- Do not execute arbitrary Python or JavaScript from condition configuration.
- Existing expression-only condition edges must remain compatible.
- Exact Slash commands must not call intent classification.
- Natural-language input must enter the intent node and route directly to a fixed Skill branch.
- `homepage-main -> Portfolio Content Agent -> explicit portfolio-* Skill` remains the execution direction.
- The jobs branch keeps its read-only job snapshot input.
- Do not restart or stop the existing 8788 and 8801 services until verification requires loading the changed code.

---

### Task 1: Structured Condition Evaluation

**Files:**
- Modify: `src/flow_studio/template.py`
- Modify: `src/flow_studio/engine.py`
- Modify: `src/flow_studio/graph.py`
- Unit tests: `tests/test_flow_template.py`
- Unit tests: `tests/test_flow_engine.py`
- Unit tests: `tests/test_flow_graph.py`

**Interfaces:**
- Consumes: Flow namespace dictionaries and condition edge metadata.
- Produces: `resolve_path(path, namespace)` and structured edge evaluation with raw-expression fallback.

- [x] **Step 1: Expose safe namespace path resolution**

Add a public resolver that accepts paths such as `input.message` and returns a missing sentinel outcome without evaluating code.

- [x] **Step 2: Implement typed comparison evaluation**

Evaluate `equals`, `not_equals`, `contains`, `not_contains`, numeric ordering operators, and `else`. Treat invalid types or missing paths as non-matches.

- [x] **Step 3: Keep legacy raw expressions working**

Use structured metadata when present; otherwise evaluate the existing `branch` expression with `eval_expr`.

- [x] **Step 4: Validate structured condition metadata**

Reject unsupported operators and invalid value types during graph validation while accepting historical expression-only edges.

- [x] **Step 5: Add focused condition tests**

Cover string equality/contains, numeric comparison, booleans/null, missing values, invalid metadata, fallback, and raw-expression compatibility.

### Task 2: Condition Builder UI

**Files:**
- Modify: `src/flow_studio/graph.py`
- Modify: `src/flow_studio/web/app.js`
- Modify: `src/flow_studio/web/style.css` only if existing field styles cannot express the layout.

**Interfaces:**
- Consumes: condition node `params.source` and structured condition edge fields.
- Produces: editable source path plus operator/value/type controls in the node and edge inspectors.

- [x] **Step 1: Add condition source metadata**

Expose `source` in the condition node form with default `input.message`.

- [x] **Step 2: Replace the raw-only branch modal**

Provide operator, value type, and comparison value controls, an `else` action, and an advanced-expression mode.

- [x] **Step 3: Render readable edge labels**

Display comparisons such as `== "/works"` while keeping advanced expressions unchanged.

- [x] **Step 4: Add node-inspector branch overview**

List outgoing targets and comparisons under the selected condition node so the complete routing decision is visible without selecting every edge.

### Task 3: Homepage Graph Refactor

**Files:**
- Modify: `src/flow_studio/homepage_flows.py`
- Modify: `src/flow_studio/homepage.py`
- Modify: `tests/test_homepage_flow.py`
- Modify: `tests/test_flow_builtin.py`

**Interfaces:**
- Consumes: Slash commands, intent definitions, Portfolio Content Agent Skill allowlist, and the existing jobs snapshot action.
- Produces: a new published `homepage-main` revision with visible fixed branches.

- [x] **Step 1: Build explicit Skill nodes**

Create fixed nodes for the six top-level Skills and every project Skill; remove dynamic `{{route.skill}}` dispatch.

- [x] **Step 2: Add the Slash condition node**

Set `source=input.message`; add exact command edges for `/help`, `/whoami`, `/works`, `/jobs`, `/job`, `/timeline`, `/contact`, and every `/project <id>` command; send `else` to intent.

- [x] **Step 3: Add the natural-language intent node**

Configure top-level and project intents with descriptions and samples, and connect each intent directly to its fixed Skill branch. Route `else` to help.

- [x] **Step 4: Preserve the jobs snapshot path**

Both `/jobs` and the `jobs` intent enter `job_snapshot -> Jobs Skill`.

- [x] **Step 5: Update public graph validation and migration**

Allow the new condition/intent topology, remove the route action requirement from the current graph, bump the revision, and publish the migrated governed snapshot.

- [x] **Step 6: Update focused homepage tests**

Assert the new node types and branch metadata, direct Slash bypass, natural-language intent routing, per-project routing, clarification/help fallback, and job context.

## Final Unit Test Verification

- [x] **Main agent: run affected unit tests after development is complete**

Run:

```bash
.venv/bin/pytest tests/test_flow_template.py tests/test_flow_engine.py tests/test_flow_graph.py tests/test_flow_builtin.py tests/test_homepage_flow.py -q
```

Expected: all tests pass. If a test fails, fix the implementation or test and rerun until it passes. Then run the complete Flow Studio test suite if the focused changes affect shared graph validation or execution behavior.
