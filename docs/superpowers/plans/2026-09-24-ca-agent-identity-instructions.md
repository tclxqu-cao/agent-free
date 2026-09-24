# CA Agent Identity Instructions Implementation Plan

> **For the main agent:** Implement this plan directly in the current session. Do not dispatch implementation or code-review subagents. After all development tasks are complete, run the affected unit tests and fix any failures before reporting completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in Flow Studio setting that includes an Agent's name and description in the instructions sent to Customer Agent.

**Architecture:** Store the boolean in the external Agent orchestration configuration because it changes Flow-to-CA request composition. Build the final instructions in the Flow Studio runtime immediately before the public CA Agent Loop request; keep `agent_id` unchanged so no named CA Agent is created or bound.

**Tech Stack:** Python 3, vanilla JavaScript, FastAPI-served static UI, pytest

## Global Constraints

- The checkbox defaults to unchecked for existing and newly created Agents.
- Unchecked requests send only the existing `system` value.
- Checked requests send name, optional description, and System prompt in a stable text format.
- `orchestration.agent_id` remains unchanged and may remain empty.
- Preserve unrelated changes in the dirty worktree.

---

### Task 1: Persist And Compose Identity Instructions

**Files:**
- Modify: `src/flow_studio/agentrt.py`
- Unit tests: `tests/test_flow_platform.py`

**Interfaces:**
- Consumes: normalized Agent dictionaries with `orchestration.include_identity_instructions: bool`
- Produces: `_external_agent_instructions(agent: dict) -> str`

- [x] **Step 1: Preserve the opt-in value during normalization**

Add `include_identity_instructions` to the external orchestration object with `False` as the default.

- [x] **Step 2: Compose the CA instructions at the protocol boundary**

```python
def _external_agent_instructions(agent: dict) -> str:
    system = str(agent.get("system") or "")
    orchestration = agent.get("orchestration") or {}
    if not orchestration.get("include_identity_instructions"):
        return system
    parts = [f"智能体名称：{str(agent.get('name') or agent.get('id') or '').strip()}"]
    description = str(agent.get("description") or "").strip()
    if description:
        parts.append(f"智能体描述：{description}")
    if system.strip():
        parts.append(f"System 提示词：\n{system.strip()}")
    return "\n\n".join(parts)
```

Use this helper for `CustomerAgentProvider.run(..., instructions=...)` without changing `agent_id`.

- [x] **Step 3: Cover opt-in and backward-compatible behavior**

Assert that unchecked Agents still send only `system`, checked Agents send the exact composed text, and the normalized flag persists while `agent_id` remains empty.

### Task 2: Expose The Checkbox In The Agent Editor

**Files:**
- Modify: `src/flow_studio/web/app.js`
- Unit tests: `tests/test_flow_server.py`

**Interfaces:**
- Consumes: `agent.orchestration.include_identity_instructions`
- Produces: `orchestration.include_identity_instructions` in the Agent save payload

- [x] **Step 1: Render the setting in the Customer Agent section**

Add an unchecked-by-default checkbox with label `将名称和描述加入 CA System 提示词` and a hint that this only changes request instructions and does not create a CA Agent.

- [x] **Step 2: Save the checkbox value**

Read `#ag-ca-include-identity` when saving and place the boolean directly on the external orchestration object.

- [x] **Step 3: Add the static UI contract assertion**

Assert that the served JavaScript contains the checkbox id and persisted property name.

### Task 3: Document The Runtime Boundary

**Files:**
- Modify: `src/flow_studio/README.md`

**Interfaces:**
- Consumes: the implemented opt-in behavior
- Produces: current project documentation for operators and maintainers

- [x] **Step 1: Document the opt-in instruction composition**

Explain that external Agents use the public CA Agent Loop, that the checkbox optionally prefixes identity text, and that it does not create or bind a named CA Agent.

## Final Unit Test Verification

- [x] **Main agent: run affected unit tests after development is complete**

Run: `.venv/bin/pytest -q tests/test_flow_platform.py tests/test_flow_server.py tests/test_homepage_flow.py`

Expected: PASS

If a test fails, fix the implementation or test and rerun this command until it passes. Report the command and result in the final response.
