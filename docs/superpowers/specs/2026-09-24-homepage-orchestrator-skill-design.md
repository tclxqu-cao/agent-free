# Homepage Orchestrator Skill Design

## Status

Approved in conversation on 2026-09-24. This design replaces the homepage's
command-by-command Flow graph with one Customer Agent node whose activated
orchestration Skill composes other installed Skills inside a single AgentLoop.

## Goal

The homepage accepts either arbitrary natural language or a stable slash command.
Every request goes to one configured homepage Agent. The Agent translates a slash
command into a concrete task, chooses and loads the required downstream Skills,
queries the appropriate source, improves the content, renders a validated artifact,
and returns it to the homepage.

The Flow graph is intentionally limited to:

```text
Homepage input -> Prompt decision -> Homepage Agent -> Homepage output
```

The decision node deterministically evaluates its configured expression/output
rules and emits the first matching task prompt. One downstream edge carries that
output to the Agent node. Knowledge retrieval, content shaping, and rendering
remain Agent behavior configured through Skills rather than separate Flow branches.

## Agent Configuration

Flow Studio owns one external Agent definition named `homepage-agent`. It uses the
configured Customer Agent model and keeps the current browser-page session ID for
the life of that page. A refresh creates a new session.

The Agent configuration enables these Skills:

- `homepage-orchestrator`: the only Skill explicitly activated by the Flow node.
- `wiki-query`: retrieves supporting material from the complete configured Obsidian
  Wiki.
- `homepage-content-polish`: turns retrieved evidence into concise, audience-aware
  content without losing sources or inventing facts.
- `homepage-content-render`: converts the polished content into the public artifact
  contract with text, sanitized HTML, images, and videos.
- Existing specialist Skills needed by the command, including the read-only jobs
  path and any retained project-specific Skills.

The Agent also enables the existing tools required by those selected Skills. This
design does not change or replace `public_wiki_query`; it remains available for
backward compatibility and is not the orchestration mechanism.

Customer Agent already makes `skill_load` available when Skills are enabled. The
orchestrator uses exact Skill names, so it does not depend on semantic Skill
discovery to choose the three core stages.

## Orchestrator Behavior

`homepage-orchestrator` receives the task prompt produced by the Flow decision and
the page session ID.
It performs these steps in one AgentLoop run:

1. Normalize the request into a task brief.
2. Load and follow the appropriate content-query Skill.
3. Load `homepage-content-polish` and apply it to the retrieved evidence.
4. Load `homepage-content-render` and produce the final artifact.
5. Return exactly one valid artifact object and no surrounding Markdown.

The orchestrator treats downstream Skill output and Wiki pages as data. Instructions
found inside retrieved content cannot change the orchestration order, load extra
capabilities, or bypass the final artifact validator.

The stages are visible through Customer Agent tool events: each `skill_load`, data
tool call, failure, and final response remains available in the Flow run log even
though the canvas contains one Agent node.

## Command Translation

Slash commands are aliases for natural-language task briefs. They are not separate
content execution branches. The prompt decision contains the command semantics
below as ordered expression/output rules. The first true expression selects its
task-prompt template. A default output appends unmatched natural language inside an
explicitly untrusted visitor-input section. The decision node never calls an LLM.

| Input | Task passed through the orchestrator |
| --- | --- |
| `/help` | Explain the available homepage commands and examples. |
| `/whoami` | Query and present the owner's current introduction. |
| `/works` | Query and present the project catalog. |
| `/project <name>` | Query detailed material for the named project. |
| `/timeline` | Query and present the project and career timeline. |
| `/contact` | Query and present contact information. |
| `/jobs` or `/job` | Read the latest successful job snapshot and present it; never trigger a public scrape or application action. |

Unknown slash commands produce a helpful command response. Arbitrary natural
language remains unchanged as the semantic task brief; the Agent decides which
enabled query Skill is relevant. Pure conversation may answer directly, while
questions that depend on stored facts must query the Wiki before answering.

## Content Contracts

The query stage returns structured evidence containing the normalized question,
answer candidates, bounded excerpts, vault-relative source paths, and any approved
media references. It does not return presentation HTML.

The polish stage returns semantic content: title, summary, ordered sections, media,
suggestions, sources, and a render preset. It may shorten and reorganize evidence,
but it cannot add unsupported facts or remove source attribution.

The render stage returns `PortfolioArtifactV1`. Supported blocks remain `text`,
`html`, `image`, and `video`. HTML uses only the existing sanitizer allowlist and
shared artifact classes. Render presets choose reusable structures such as article,
project grid, timeline, project detail, and chat; the browser owns the corresponding
CSS.

## Flow Graph

`homepage-main` contains four nodes:

1. `start`: accepts `message` and `session_id`.
2. `condition`: reads `input.message`, selects the first matching expression/output
   rule, and renders that rule's task prompt. Exact commands use equality
   expressions, `/project <name>` uses a membership expression and includes the
   original input, and `default_output` wraps arbitrary natural language as
   untrusted visitor input.
3. `ai_agent`: selects `homepage-agent`, activates `homepage-orchestrator`, sends
   `{{prompt_decision.text}}`, and reuses `{{input.session_id}}`.
4. `end`: returns the Agent's final artifact text.

The graph contains no per-command execution node, intent node, per-project node,
chat node, or server formatting node. The prompt decision only emits text; it never
chooses a different Agent or content implementation. The published graph and its
generated default are kept identical.

The generic condition-node contract gains an optional `outputs` mode configured on
the node itself:

```json
{
  "source": "input.message",
  "outputs": [
    {"name": "contact", "expression": "input.message == '/contact'", "output": "查询并整理联系方式"},
    {"name": "project", "expression": "'/project ' in input.message", "output": "查询项目资料：{{input.message}}"}
  ],
  "default_output": "处理访客请求：{{input.message}}"
}
```

The node evaluates rules in order and returns `matched`, `text`, and the selected
rule name. In outputs mode it has one ordinary downstream edge. The Flow editor
exposes a repeatable `判断表达式 / 输出模板` editor plus `默认输出`. Existing
conditions without `outputs` retain their current edge-based routing behavior.

## Homepage Runtime

The homepage server sends the original input to `homepage-main` and validates the
returned artifact before exposing it to the browser. It does not select a content
Skill before starting the Flow.

Exact commands may continue to use last-success snapshots for static export and
failure fallback. Arbitrary natural-language replies are live session results and
must not fall back to an unrelated cached answer. The orchestrator's canonical
output kind supplies the cache key after a successful run; a small exact-command
fallback map may be retained only for recovering the corresponding prior artifact
when the live run fails.

The browser's current queue, loading message, session lifetime, artifact renderer,
HTML sanitizer, media viewer, and live Flow progress UI remain in use.

## Error Handling

- A missing or disabled downstream Skill fails the run with the exact Skill name.
- Query failure stops evidence-based content generation; the model cannot substitute
  an invented answer.
- Polish or render output that violates its stage contract is rejected.
- Invalid final JSON, unsupported blocks, unsafe URLs, and unsafe HTML are rejected
  by the existing server and Flow validators.
- `/jobs` reads only the last successful real snapshot; missing or stale data is
  reported explicitly.
- Exact-command fallback never substitutes a different command's artifact.

## Validation

The implementation must prove:

- Customer Agent discovers and loads all four core Skills.
- The orchestrator loads query, polish, and render Skills in order.
- `/contact` and `/project agentroam` traverse the three stages and return valid
  artifacts with sources.
- A natural-language project question uses the same single Agent node.
- A greeting completes without a forced Wiki query.
- `/jobs` reads the real snapshot without starting a scrape.
- A malicious instruction inside a Wiki excerpt cannot alter the Skill sequence or
  emit unsafe HTML.
- Refresh creates a new session while multiple messages on one page reuse the same
  session.
- The published `homepage-main` graph contains exactly start, prompt decision,
  homepage Agent, and end nodes; the decision has one edge to that Agent; live run
  events show the selected prompt plus downstream Skill/tool activity.
- Existing condition-only Flow graphs keep their previous branch behavior when no
  node output rules are configured.
- Existing desktop/mobile layouts render text, HTML, images, and videos without page
  overflow.

## Non-goals

- Keeping separate Flow branches for every command or project.
- Hard-coding project prose, contact details, or rendered HTML in the homepage
  service.
- Letting a public visitor choose the Agent, model, Skill IDs, tools, MCP servers, or
  memory policy.
- Triggering job applications, Wiki writes, or live recruitment-site scraping from
  the public homepage.
