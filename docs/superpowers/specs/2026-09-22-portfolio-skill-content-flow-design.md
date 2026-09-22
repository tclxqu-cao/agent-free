# Portfolio Skill Content Flow Design

## Status

Approved in conversation on 2026-09-22. This document defines the implementation
boundary across Flow Studio, Customer Agent, and the static portfolio.

## Context

The portfolio currently keeps public content in two hand-maintained forms:

- `/Users/caoqu/.zcode/workspace/default/portfolio/index.html` contains project,
  timeline, contact, and capability templates.
- `src/flow_studio/portfolio_content.json` is exported from those templates and
  loaded by the homepage Flow service.
- `/Users/caoqu/.zcode/workspace/default/portfolio/media-map.js` maps a subset of
  projects to local images and videos.

This duplicates content, makes knowledge-base updates invisible until code is
edited, and prevents the Flow graph from showing the real command path.

Customer Agent already provides a ReAct AgentLoop, editable skills, an AIHub
provider, and HTTP/SSE run APIs. The local service is currently configured with
the `aihub/deepseek` profile. The generic Agent is not suitable for a public
homepage because it also exposes general-purpose tools. The homepage therefore
needs a dedicated, capability-limited content Agent entrypoint.

## Goals

1. Treat every public content unit as an editable Customer Agent Skill.
2. Execute content Skills with the user's Customer Agent through AIHub.
3. Query approved public knowledge-base pages instead of hand-written frontend
   templates or duplicated backend JSON.
4. Keep every explicit slash command visible as a deterministic Flow branch.
5. Route natural language to the same Skills through intent recognition.
6. Return text, image, video, and sanitized HTML blocks through one contract.
7. Keep the homepage usable when Customer Agent or AIHub is offline by serving
   the most recent successful generated artifact.
8. Allow static media, generated diagrams, and content snapshots to move to
   Vercel without changing command or rendering logic.

## Non-goals

- Exposing the user's general Customer Agent, shell, filesystem, MCP, scheduling,
  memory writes, or private knowledge base to public visitors.
- Generating all homepage content live on every browser refresh.
- Letting arbitrary model output execute scripts or inject unrestricted HTML.
- Publishing private screenshots, data, or implementation details from internal
  systems.
- Replacing the existing job collection and matching pipeline.

## Chosen Architecture

Use **Skill generation with a last-success artifact cache**.

```text
Portfolio browser
  -> configurable homepage Flow endpoint
    -> deterministic slash router OR natural-language intent router
      -> homepage Skill branch
        -> last-success artifact store
          -> optional authenticated refresh
            -> Customer Agent public Skill endpoint
              -> Portfolio Content Agent
                -> AIHub provider
                -> public_wiki_query only
```

Public reads return generated artifacts quickly. A missing or stale artifact can
trigger a bounded refresh. A refresh failure leaves the previous artifact intact.
The browser never calls Customer Agent directly.

## Repository Responsibilities

### Flow Studio (`/Users/caoqu/agent-free`)

- Own the public homepage API, command routing, natural-language routing, rate
  limits, artifact validation, and last-success artifact store.
- Represent the shared `Portfolio Content Agent` as a selectable Flow Studio
  Agent definition. Every homepage content branch uses an Agent node bound to
  that definition and passes only its Skill ID and public input context.
- The bound Agent adapter calls the authenticated Customer Agent public Skill
  endpoint for generation; Flow execution does not bypass the canvas graph.
- Keep one visible Flow branch per top-level public command.
- Preserve the existing real job data source while exposing it to the dedicated
  Skill as validated read-only branch context.
- Export generated artifacts for static/Vercel fallback.

### Customer Agent (`/Users/caoqu/team-agent/customer-agent`)

- Own the dedicated `Portfolio Content Agent` and Skill execution endpoint.
- Force the configured AIHub profile for this Agent.
- Expose only explicitly allowed public tools to this execution mode.
- Load exactly the requested Skill; do not use open-ended semantic Skill
  discovery for explicit commands.
- Stream Agent events and return a validated final artifact envelope.

### Portfolio (`/Users/caoqu/.zcode/workspace/default/portfolio`)

- Own terminal interaction, slash palette, Skills submenu, streaming display,
  sanitized block rendering, media lightbox, and Vercel asset base configuration.
- Contain no project, timeline, contact, or capability prose as an authoritative
  data source.
- Use the exported last-success snapshot only when the configured Flow endpoint
  is unreachable.

## Skill Catalog

Top-level Skills:

| Public command | Customer Agent Skill | Purpose |
| --- | --- | --- |
| `/help` | `portfolio-help` | Describe available public commands |
| `/whoami` | `portfolio-whoami` | Generate the public personal introduction |
| `/works` | `portfolio-works` | List projects and summarize demonstrated capabilities |
| `/jobs` | `portfolio-jobs` | Query and present the latest real job snapshot |
| `/timeline` | `portfolio-timeline` | Build a timeline from public project knowledge, later preferring a public resume page |
| `/contact` | `portfolio-contact` | Return public WeChat official account, GitHub, and other approved contact links |

`/job` is accepted as an alias for `/jobs`; the visible canonical command is
`/jobs`. `skills` is a menu label only and is never sent as a command. Its menu
contains `/works` and `/jobs`.

Each project detail is also an editable Skill, using the stable naming convention
`portfolio-project-<project-id>`. Initial project Skills cover:

- `agentroam`
- `knowledge-base`
- `smart-refund`
- `agent-swarms`
- `quality-platform`
- `customer-service`
- `flow-studio`
- `meitu-web`
- `vibe-works`
- `kid-earth`

The project list is generated from discoverable `portfolio-project-*` Skills and
their public metadata. It is not maintained as a second HTML list.

`/clear` remains a local terminal control because it has no content and does not
need a Flow or Skill invocation.

## Natural-language Routing

Explicit slash commands bypass intent recognition and go directly to their fixed
branch. Natural-language input uses deterministic public rules first, followed by
AIHub classification only when rules do not resolve the intent.

| Natural-language meaning | Destination |
| --- | --- |
| project, work, what have you built, what do you do | `/works` |
| job, position, recruitment, job hunting, application | `/jobs` |
| work experience, experience, resume, career history, manager experience | `/timeline` |
| contact, WeChat official account, GitHub, how to reach you | `/contact` |
| help, how to use this, available functions | `/help` |
| a known project name or alias | matching `portfolio-project-*` Skill |

The Chinese word `工作` alone routes to `/works`. It routes to `/jobs` only when
the request contains explicit recruitment semantics such as `岗位`, `招聘`,
`职位`, `找工作`, or `应聘`.

If classification confidence is below the configured threshold or two intents
remain plausible, the Flow returns a clarification response instead of guessing.

## Flow Graph

`Portfolio Content Agent` is created once on the Agent tab. Its model profile is
AIHub DeepSeek and its capabilities contain only the `portfolio-*` Skills and
the approved public read tools. The Flow canvas does not duplicate this
configuration: each content Agent node selects the same Agent and supplies a
different `skillId`.

The main homepage graph has two entry paths:

```text
start
  -> input kind
    -> slash router
      -> help skill
      -> whoami skill
      -> works skill
      -> jobs skill
      -> timeline skill
      -> contact skill
      -> project skill dispatcher
    -> natural-language intent
      -> same Skill branches
      -> clarification
  -> artifact normalizer
  -> end
```

Each slash branch is visible on the Flow Studio canvas. Natural-language routing
reuses those branches rather than maintaining separate answer implementations.
The `/jobs` branch first invokes the existing `homepage-jobs` read-only workflow
to obtain the current job snapshot, then passes that public data to the shared
Agent node with `skillId=portfolio-jobs`. Other branches call their Skill Agent
node directly. Project selection uses a project Skill dispatcher that resolves
only registered `portfolio-project-*` Skills.

The governed published `homepage-main` version must be migrated or republished;
changing only the built-in seed is not sufficient for an already-published graph.

## Customer Agent Public Skill Endpoint

Add a dedicated server endpoint for Flow Studio, separate from the generic
`/api/agent/run` route. The endpoint accepts:

```json
{
  "skill": "portfolio-works",
  "input": "工作",
  "context": {
    "locale": "zh-CN",
    "jobSnapshot": null
  }
}
```

Requirements:

- Authenticate with a dedicated bearer token shared only with Flow Studio.
- Allow only `portfolio-*` Skills.
- Select the configured `Portfolio Content Agent` and AIHub profile.
- Build an isolated AgentLoop without generic session tools.
- Expose only `skill_load` and `public_wiki_query`. The jobs branch supplies its
  validated read-only job snapshot through the request context instead of giving
  the public Agent direct database access.
- Apply concurrency, timeout, input-length, and output-size limits.
- Return SSE events for progress and a final artifact object.
- Reject unknown Skills, malformed artifacts, unsafe URLs, and attempts to request
  capabilities outside the allowlist.

The general Agent API remains unchanged and is not made public.

## Public Knowledge Boundary

The public query tool is fail-closed. Untagged Wiki pages are not public by
default for this endpoint.

Each Skill declares an allowlist of public knowledge page paths or an explicitly
maintained public collection. `public_wiki_query` can search and read only within
that allowlist. It returns source identifiers with excerpts so the Skill can
retain provenance in generated metadata.

The timeline Skill initially uses public project pages. When a public resume page
is later added to the knowledge base, the Skill prefers it automatically and uses
project pages as supporting evidence. No frontend or Flow change is required.

The contact Skill reads a public profile/contact page. Missing WeChat or GitHub
data produces an honest partial response; the model must not invent it.

## Artifact Contract

Every Skill returns one versioned envelope:

```json
{
  "schemaVersion": 1,
  "skill": "portfolio-project-agentroam",
  "title": "AgentRoam",
  "summary": "Multi-agent workspace across desktop, web, and mobile.",
  "blocks": [
    { "type": "text", "text": "...", "tone": "body" },
    { "type": "image", "src": "https://example.vercel.app/media/agentroam.png", "alt": "..." },
    { "type": "video", "src": "https://example.vercel.app/media/agentroam.mp4", "poster": "https://example.vercel.app/media/agentroam.jpg" },
    { "type": "html", "html": "<section>...</section>" }
  ],
  "suggestions": ["/works", "/contact"],
  "sources": ["projects/customer-agent/customer-agent.md"],
  "generatedAt": "2026-09-22T00:00:00Z"
}
```

Supported block types are `text`, `image`, `video`, and `html`. Routing metadata,
Skill identifiers, and schema definitions are configuration, not business
content, and may remain in code.

HTML is a fragment, not a document. It is sanitized with a strict allowlist on
the server and defensively sanitized again in the browser. Scripts, styles,
iframes, forms, event-handler attributes, unsafe protocols, embedded credentials,
and arbitrary remote origins are rejected. Links receive safe target/rel values.

## Artifact Lifecycle and Fallback

1. A command branch resolves its Skill ID.
2. The Flow reads the latest valid artifact.
3. If no artifact exists, the request performs one bounded live generation.
4. If an artifact is stale, the Flow returns it immediately with freshness
   metadata and starts at most one deduplicated refresh.
5. A successful refresh validates and atomically replaces the artifact.
6. A failed refresh preserves the previous artifact and records the failure.
7. Export produces a static snapshot consumed by Vercel when the Flow endpoint is
   unreachable.

Public visitors cannot force refreshes. An authenticated maintenance endpoint or
local CLI performs explicit regeneration. This prevents homepage refreshes from
flooding AIHub.

## Media and Diagram Rules

Media resolution order is:

```text
video -> image -> generated project flow diagram
```

Existing video/image projects keep their current media until knowledge-base
metadata supplies Vercel URLs. Projects without either receive a generated flow
diagram:

- AgentRoam
- personal knowledge base
- smart refund / Small WOW
- Agent Swarms
- quality platform
- customer service and ticketing system

Internal projects use abstract process diagrams only. They must not include real
screenshots, production data, internal hostnames, credentials, employee data, or
private system identifiers.

Generated diagrams are sanitized HTML/SVG fragments, or exported static
image/HTML assets referenced by approved Vercel origins. Media URLs are data in
the generated artifact, not a second manually maintained JavaScript map.

## Portfolio Interaction

- All visible command labels are English slash commands.
- Hovering, focusing, or clicking `skills` opens a stable submenu with `/works`
  and `/jobs`.
- Typing `/` opens the complete public command palette.
- Arrow keys move through suggestions; Enter runs; Escape closes.
- Initial page load cycles through the public commands and streams each artifact
  into the terminal output.
- Any keyboard input, pointer interaction, focus interaction, or manual output
  scroll calls `streamFinish()` and stops the automatic cycle.
- `prefers-reduced-motion` renders complete results without typewriter animation.
- Terminal colors distinguish prompts, commands, headings, body text, metadata,
  links, success, warnings, and errors while preserving readable contrast.
- Streaming uses absolute elapsed time and hidden final-height placeholders so it
  remains correct at high refresh rates and does not shift the layout.

## Error Handling

- Flow unavailable: render the exported last-success static snapshot.
- Customer Agent or AIHub unavailable during refresh: retain the previous
  artifact and show its generation time without exposing internal errors.
- No prior artifact and generation fails: return a typed unavailable block with
  retry guidance; do not restore hand-written prose.
- Knowledge page missing: return an explicit missing-information block.
- Invalid or unsafe HTML/media: reject the new artifact and retain the last valid
  version.
- Job snapshot missing or stale: preserve the existing truthful job date and
  state; never substitute demo jobs for visitors.

## Testing and Acceptance

### Customer Agent

- Endpoint authentication and Skill allowlist tests.
- Tool-capability isolation tests proving Shell, file writes, MCP, cron, memory
  writes, dispatch, and ask-user are absent.
- Exact Skill activation and AIHub profile-selection tests.
- Artifact parsing, sanitizer, timeout, concurrency, and output-limit tests.
- A real local AIHub execution for at least one Skill when the desktop relay is
  available.

### Flow Studio

- Each slash command reaches its fixed graph branch without classification.
- Natural-language routing covers the mapping table, including `工作` -> works
  and recruitment terms -> jobs.
- Low-confidence input reaches clarification.
- Artifact refresh is deduplicated and atomically replaces only valid results.
- Published governed graph matches the new branch topology.
- Existing job filtering continues to exclude demo data.

### Portfolio

- Slash palette, keyboard navigation, Skills submenu, and command aliases.
- Initial command cycle, interruption behavior, reduced motion, and high-refresh
  streaming behavior.
- Rendering of text, image, video, and sanitized HTML blocks.
- Flow failure fallback to exported generated content.
- Desktop and mobile visual verification through ego-browser, including output
  typography, no overlap, no unexpected page scroll, and media lightbox behavior.

## Migration

1. Introduce and test the artifact schema and sanitization.
2. Add the dedicated Customer Agent public Skill runner and safe query tools.
3. Add/import the Portfolio Content Agent and `portfolio-*` Skills.
4. Generate initial artifacts from approved public knowledge sources.
5. Replace homepage command logic with fixed Skill branches and natural-language
   routing to those branches.
6. Replace frontend templates and media map with artifact rendering and static
   snapshot fallback.
7. Publish or migrate the governed homepage Flow version.
8. Verify local services on the existing shared ports and run end-to-end browser
   acceptance.
9. Keep old generated JSON/templates only until the new artifact path has passed
   end-to-end verification, then remove them in a separate reviewed change.
