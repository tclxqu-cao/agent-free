# Portfolio Skill Content Flow Implementation Plan

> **For the main agent:** Implement this plan directly in the current session. Do not dispatch implementation or code-review subagents. After all development tasks are complete, run the affected unit tests and fix any failures before reporting completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hand-written portfolio content with Customer Agent Skills executed through AIHub, route slash commands and natural language through visible Flow branches, render multimodal artifacts in the terminal, and expose the verified site on the Mac's `100.x` address.

**Architecture:** Flow Studio owns public routing, the job snapshot, and a last-success artifact cache. A Flow Studio `ai_agent` node binds the shared Portfolio Content Agent and calls a dedicated Customer Agent Skill endpoint that builds an isolated AgentLoop using AIHub and only a public Wiki read tool. The static portfolio renders validated artifact blocks and falls back to the exported last-success snapshot when the Flow endpoint is unavailable.

**Tech Stack:** Python 3.11, FastAPI, pytest, TypeScript, Next.js 14 route handlers, `@agent/core`, AIHub Provider, Vitest, vanilla HTML/CSS/JavaScript, Node test runner.

## Global Constraints

- Preserve all existing dirty-worktree changes in all three repositories.
- Canonical visible commands are `/help`, `/whoami`, `/works`, `/jobs`, `/timeline`, and `/contact`; `/job` is an alias.
- `skills` is a hover/click/focus menu label only and expands to `/works` and `/jobs`.
- Explicit slash commands bypass intent classification and enter fixed Flow branches.
- The Chinese word `工作` routes to works unless explicit recruitment terms are present.
- No project, timeline, contact, or capability prose remains authoritative in frontend templates or backend duplicate JSON.
- Public Skill execution may read only explicitly approved Wiki pages and may not expose shell, file writes, MCP, cron, memory writes, dispatch, or ask-user tools.
- HTML blocks are sanitized on the server and defensively sanitized in the browser.
- AIHub failure preserves the previous valid artifact; public browser requests cannot force refreshes.
- Internal-system diagrams contain no screenshots, production data, hostnames, credentials, employee data, or private identifiers.

---

### Task 1: Customer Agent Public Skill Runner

**Files:**
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/portfolio-skill-defaults.ts`
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/portfolio-skill-runner.ts`
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/portfolio-artifact.ts`
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/app/api/portfolio/skills/run/route.ts`
- Create: `/Users/caoqu/team-agent/customer-agent/packages/server/lib/portfolio-skill-runner.test.ts`
- Modify: `/Users/caoqu/team-agent/customer-agent/packages/server/package.json`
- Modify: `/Users/caoqu/team-agent/customer-agent/bun.lock`

**Interfaces:**
- Consumes: `AgentBuilder`, `businessCatalog().skills`, persisted model profiles, `AiHubProvider`, and the public Wiki root.
- Produces: `runPortfolioSkill(request): AsyncIterable<AgentEvent>` and `PortfolioArtifactV1` with `text | image | video | html` blocks.

- [x] **Step 1: Define and validate the artifact contract**

Create `PortfolioArtifactV1`, URL-origin validation, output limits, and `sanitizePortfolioHtml(html)` using `sanitize-html` with a narrow tag/attribute allowlist.

```ts
export interface PortfolioArtifactV1 {
  schemaVersion: 1;
  skill: string;
  title: string;
  summary?: string;
  blocks: PortfolioBlock[];
  suggestions?: string[];
  sources?: string[];
  generatedAt: string;
}
```

- [x] **Step 2: Add editable default Portfolio Skills**

Seed `portfolio-help`, `portfolio-whoami`, `portfolio-works`, `portfolio-jobs`, `portfolio-timeline`, `portfolio-contact`, and each `portfolio-project-*` Skill into `SQLiteSkillStore` only when missing. Prompts require one JSON artifact, truthful missing-data handling, and no Markdown fences.

- [x] **Step 3: Implement the fail-closed public Wiki tool**

Read only Markdown files below `${PORTFOLIO_PUBLIC_WIKI_ROOT:-$HOME/.obsidian/wiki/projects/portfolio-public}`. Resolve paths, reject symlinks/path traversal, cap bytes/results, and return matching excerpts plus vault-relative sources.

- [x] **Step 4: Build the isolated AIHub AgentLoop**

Resolve the requested persisted profile, require `provider === "aihub"`, register only the requested Skill and public Wiki tool, disable semantic matching, allow only `public_wiki_query`, and omit AgentHost session tools.

```ts
const builder = new AgentBuilder()
  .withModel("aihub", model)
  .withWorkingDirectory(publicWikiRoot)
  .withSystemPrompt(PORTFOLIO_AGENT_PROMPT)
  .withEnabledTools(["public_wiki_query"])
  .withEnabledSkills([request.skill])
  .withSemanticSkillMatching(false)
  .withMaxIterations(6);
```

- [x] **Step 5: Add the authenticated SSE route**

Require `Authorization: Bearer $PORTFOLIO_SKILL_TOKEN`, validate input length and `portfolio-*` allowlist, enforce bounded concurrency, stream Agent events, and replace the final event with the parsed/sanitized artifact.

- [x] **Step 6: Add focused unit tests**

Cover authentication, unknown Skill rejection, non-AIHub profile rejection, capability isolation, public Wiki traversal rejection, JSON-fence recovery, unsafe HTML removal, unsafe media URL rejection, and valid artifact streaming.

### Task 2: Public Wiki Content and Diagram Assets

**Files:**
- Create: `/Users/caoqu/.obsidian/wiki/projects/portfolio-public/portfolio-public.md`
- Create: `/Users/caoqu/.obsidian/wiki/projects/portfolio-public/projects/*.md`
- Create: `/Users/caoqu/.zcode/workspace/default/portfolio/assets/flows/*.svg`

**Interfaces:**
- Consumes: Current public portfolio prose, verified project Wiki pages, existing media paths, and public contact facts.
- Produces: Explicitly public knowledge pages read by CA Skills and six static project process diagrams referenced by generated artifacts.

- [x] **Step 1: Build public-only knowledge pages through `wiki-capture`**

Create a public profile/index plus one page per project. Mark every page `visibility/public`, preserve provenance, keep internal projects at abstract core-introduction level, and include media/diagram metadata without private paths.

- [x] **Step 2: Resolve public contact facts**

Search the Wiki and repository remotes for verified GitHub and WeChat official-account details. Record only proven values; explicitly mark unavailable fields instead of inventing them.

- [x] **Step 3: Create missing project flow diagrams**

Use a consistent terminal-compatible architecture style for AgentRoam, knowledge base, smart refund/Small WOW, Agent Swarms, quality platform, and customer service. Export deterministic SVG assets with readable mobile text and no internal identifiers.

- [x] **Step 4: Add media metadata to public project pages**

Use `video -> image -> diagram` priority. Existing projects keep their current local media references until Vercel base URLs are configured; missing-media projects reference the new diagrams.

### Task 3: Flow Studio External Agent and Artifact Runtime

**Files:**
- Modify: `src/flow_studio/agentrt.py`
- Modify: `src/flow_studio/bridge.py`
- Modify: `src/flow_studio/workspace.py`
- Modify: `src/flow_studio/engine.py`
- Modify: `src/flow_studio/web/app.js`
- Create: `src/flow_studio/homepage_artifacts.py`
- Modify: `src/flow_studio/homepage.py`
- Modify: `src/flow_studio/homepage_flows.py`
- Modify: `tests/test_flow_platform.py`
- Modify: `tests/test_homepage_flow.py`

**Interfaces:**
- Consumes: Customer Agent SSE endpoint, `PortfolioArtifactV1`, existing job snapshot query, Flow Studio `ai_agent` nodes and governed resources.
- Produces: External Agent execution mode, fixed homepage Skill branches, natural-language router, and atomic last-success artifacts.

- [x] **Step 1: Extend Flow Studio Agent definitions**

Add normalized fields `runtime`, `profile_id`, and allowed `skill_ids`. Seed a `portfolio-content-agent` using runtime `customer-agent`, profile `aihub-deepseek`, no memory, and only Portfolio Skills. Change seeding to add missing built-ins without replacing user edits.

- [x] **Step 2: Add Skill selection to `ai_agent` nodes**

The inspector shows only Skills bound to the selected Agent. Persist `skill_id`; pass rendered input/context into `AgentRuntime.run(..., skill_id=..., context=...)`. Reject a node Skill not bound to the Agent.

- [x] **Step 3: Add Customer Agent Skill bridge support**

Implement `agent_skill(cfg, skill, input, context, profile_id, timeout)` to POST the dedicated route, consume SSE, surface tool progress, and return the final artifact. Existing `agent_reason` behavior remains unchanged.

- [x] **Step 4: Add external Agent runtime execution**

When `agent.runtime == "customer-agent"`, bypass the local LLM/tool loop and call `agent_skill`. Preserve observability and return the artifact alongside text/steps so Flow templates can return it without string loss.

- [x] **Step 5: Implement artifact validation and atomic cache**

Create `HomepageArtifactStore` under the workspace data root. Validate schema/version/block limits, save via temporary file plus `os.replace`, preserve the previous version on failure, and export a static snapshot JSON.

- [x] **Step 6: Replace homepage routing and graph topology**

Normalize `/job` to `/jobs`; exact slash commands route without classification. Deterministic Chinese/English rules map natural language to the same branch, with `工作 -> works` unless recruitment terms appear. Build visible branches for help, whoami, works, jobs, timeline, contact, project dispatch, and clarify. Jobs read the existing real snapshot before the Agent node.

- [x] **Step 7: Migrate the governed homepage resources**

Ensure `portfolio-content-agent` and the new `homepage-main` graph are present in the default governed workspace. Create/publish a new version only when the existing published snapshot has the old topology; preserve unrelated user drafts.

- [x] **Step 8: Add focused Python tests**

Cover Agent normalization/UI metadata, Skill allowlist enforcement, bridge SSE parsing, atomic fallback, direct slash branches, natural-language mapping, job context, project dispatch, and graph node/edge topology.

### Task 4: Portfolio Terminal and Multimodal Rendering

**Files:**
- Modify: `/Users/caoqu/.zcode/workspace/default/portfolio/index.html`
- Modify: `/Users/caoqu/.zcode/workspace/default/portfolio/main.js`
- Modify: `/Users/caoqu/.zcode/workspace/default/portfolio/style.css`
- Modify: `/Users/caoqu/.zcode/workspace/default/portfolio/site-config.js`
- Replace: `/Users/caoqu/.zcode/workspace/default/portfolio/media-map.js`
- Create: `/Users/caoqu/.zcode/workspace/default/portfolio/content-snapshot.js`
- Modify: `/Users/caoqu/.zcode/workspace/default/portfolio/api/command.js`
- Modify: `/Users/caoqu/.zcode/workspace/default/portfolio/tests/command.test.js`
- Create: `/Users/caoqu/.zcode/workspace/default/portfolio/tests/ui-contract.test.js`

**Interfaces:**
- Consumes: Flow `PortfolioArtifactV1`, static snapshot, configurable `mainFlowUrl` and `assetBaseUrl`.
- Produces: English command UI, Skills submenu, slash palette, interrupted auto-demo, and safe multimodal terminal output.

- [x] **Step 1: Remove authoritative content templates**

Keep only the terminal shell and minimal no-JS notice in HTML. Remove project/timeline/contact/capability prose templates and load the generated snapshot before `main.js`.

- [x] **Step 2: Add the English command registry and Skills menu**

Use one registry for visible chips, slash suggestions, aliases, and auto-demo. `skills` is a disclosure control; it opens `/works` and `/jobs` on hover, focus, or click. Typing `/` shows all public commands with arrow/Enter/Escape support.

- [x] **Step 3: Render and sanitize artifact blocks**

Render text with terminal semantic classes, approved image/video URLs with lightbox behavior, and HTML through a DOMParser allowlist sanitizer. Never assign unsanitized model output to `innerHTML`.

- [x] **Step 4: Replace command execution and fallback**

Send canonical slash commands to the configured Flow URL. Validate artifacts, fall back only to `window.PORTFOLIO_SNAPSHOT`, and remove local hand-written command content.

- [x] **Step 5: Implement initial command cycling**

On load, query every public command sequentially and stream results into the output. Stop immediately on keyboard, pointer, focus, or manual scroll; finish the active stream first. Respect reduced motion and use absolute elapsed-time streaming.

- [x] **Step 6: Improve terminal typography and semantic color**

Use a restrained neutral terminal palette with separate prompt, command, heading, body, metadata, link, success, warning, and error tokens. Keep mobile text readable, prevent overlap, and preserve a stable input/footer height.

- [x] **Step 7: Add frontend contract tests**

Test command normalization, Skills menu structure, slash registry, artifact validation, HTML sanitizer, snapshot fallback, auto-demo interruption, and video/image/diagram rendering.

### Task 5: Export, Runtime, and End-to-End Acceptance

**Files:**
- Modify: `scripts/export_portfolio.py`
- Modify: `scripts/preview_homepage.py`
- Modify: `/Users/caoqu/.zcode/workspace/default/portfolio/README.md`

**Interfaces:**
- Consumes: Valid Flow artifacts, media base URL, services on ports 3000/8788/8801, and the Mac Tailscale IPv4 address.
- Produces: Static Vercel-ready snapshot, a single shared local preview stack, and a phone-accessible `http://100.x.x.x:8801/` URL.

- [x] **Step 1: Export generated artifacts instead of HTML templates**

Write deterministic `content-snapshot.js` and media metadata from the Flow artifact store. Do not recreate `portfolio_content.json` from HTML.

- [x] **Step 2: Update preview orchestration**

Reuse existing live 3000 and 8788 services when healthy, stop stale duplicate preview processes, bind portfolio preview to `0.0.0.0:8801`, and preserve configurable URLs/tokens.

- [x] **Step 3: Run all affected unit tests and fix failures**

Run the Customer Agent Vitest files, Flow Studio pytest files, and portfolio Node tests after all development is complete.

- [x] **Step 4: Start and verify the complete stack**

Verify Customer Agent AIHub status, Flow health, homepage command responses, fixed graph branches, cached fallback, and HTTP access on loopback plus the Tailscale `100.x` address.

- [x] **Step 5: Perform browser acceptance with `ego-browser`**

Verify desktop and mobile viewports: English command chips, Skills submenu, slash palette, streaming cycle, interruption, project diagrams, image/video playback, HTML rendering, typography, no overlap, and no unexpected body scroll.

- [x] **Step 6: Update the Wiki with the implemented architecture**

Use `wiki-capture` for the reusable CA Skill/Flow/cache security pattern and include exact verified runtime boundaries.

## Final Unit Test Verification

- [x] **Main agent: run affected unit tests after development is complete**

Run:

```bash
cd /Users/caoqu/team-agent/customer-agent && bunx vitest run packages/server/lib/portfolio-skill-runner.test.ts
cd /Users/caoqu/agent-free && .venv/bin/pytest tests/test_homepage_flow.py tests/test_flow_platform.py -q
cd /Users/caoqu/.zcode/workspace/default/portfolio && node --test tests/*.test.js
```

Expected: all commands pass. If a test fails, fix the implementation or test and rerun until it passes. Report the commands and results in the final response.
