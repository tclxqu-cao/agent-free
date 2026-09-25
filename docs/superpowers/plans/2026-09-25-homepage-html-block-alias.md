# Homepage HTML Block Alias Compatibility Implementation Plan

> **For the main agent:** Implement this plan directly in the current session. Do not dispatch implementation or code-review subagents. After all development tasks are complete, run the affected unit tests and fix any failures before reporting completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display safe homepage HTML when the model uses `text` as the HTML block payload field, while preserving the canonical artifact and security checks.

**Architecture:** Flow Studio owns Portfolio artifact validation and normalizes the one known model alias before returning the artifact. Customer Agent remains a generic AgentLoop host; its render Skill is tightened so future model output uses the canonical field.

**Tech Stack:** Python 3.12, pytest, Customer Agent file-backed SKILL.md.

## Global Constraints

- Do not change the public `PortfolioArtifactV1` contract: canonical HTML payload remains `html`.
- Do not bypass or weaken the existing HTML length and active-content checks.
- Do not modify Customer Agent runtime data under `packages/desktop/.agent-data/` or `.agent/skills/wiki-query/log.md`.

---

### Task 1: Flow Studio Artifact Normalization

**Files:**
- Modify: `src/flow_studio/homepage_artifacts.py:52-90`
- Unit tests: `tests/test_homepage_flow.py`

**Interfaces:**
- Consumes: `validate_artifact(value: object, expected_skill: str | None = None) -> dict`
- Produces: canonical HTML blocks containing `html` and no `text` alias

- [x] **Step 1: Normalize the HTML payload alias**

Read `block.html` first, otherwise read `block.text`; apply the existing size and unsafe-markup expression to that value. Store the validated value as `clean_block["html"]` and remove `clean_block["text"]`.

- [x] **Step 2: Add focused regression tests**

Add one assertion that safe HTML in `text` becomes canonical `html`, and one assertion that `<script>` supplied through `text` still raises `ValueError`.

### Task 2: Customer Agent Render Skill Contract

**Files:**
- Modify: `/Users/caoqu/team-agent/customer-agent/.agent/skills/homepage-content-render/SKILL.md`

**Interfaces:**
- Consumes: `PortfolioArtifactV1` block contract
- Produces: model instruction containing the exact HTML block shape

- [x] **Step 1: Add the canonical HTML example**

State that HTML blocks use `{"type":"html","html":"<div class='artifact-panel'>...</div>"}` and must never place HTML in `text`.

## Final Unit Test Verification

- [x] **Main agent: run affected unit tests after development is complete**

Run: `.venv/bin/pytest -q tests/test_homepage_flow.py`
Expected: PASS
