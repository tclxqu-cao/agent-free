# Digital Human Stage Design

## Goal

Add a third top-level view 数字人 (Digital Human Stage) to the Flow Studio web app. Every node of a flow is presented as a digital human figure; while a flow runs, the stage shows each human's live state and animates messages traveling along the graph edges; a message feed shows every pass-through message with its content. The figures themselves are digital humans, not abstract boxes.

Constraints: zero-build vanilla JS/CSS/SVG only (no framework, no external assets), no backend changes, no new governance surface — the view consumes the existing run/event APIs and therefore inherits existing authentication, CSRF, workspace scoping and policy enforcement.

## Data Sources (all existing)

- `GET /api/flows` — flow selector.
- `POST /api/flows/{id}/run {inputs, background:true}` — start a run (existing inputs dialog).
- `GET /api/runs?limit=…` — idle scan for the newest `running` run (auto-attach) and the recent-runs list (replay entry).
- `GET /api/runs/{id}/events?after=seq` — cursor page of `{seq, type, level, message, node_id, node_label, timestamp, traceback?}` plus the fresh `run` object.
- `run.graph` snapshot — stage topology: `nodes[{id,type,label,pos:{x,y}}]`, `edges[{from,to,branch?}]`. Runs old enough to lack a snapshot fall back to a roster layout built from `run.node_runs` (edges unknown, no particles).

## Stage

- One digital human per node, positioned by the snapshot's canvas coordinates, auto-fitted (bounding box → scale/translate, clamped). Human figure = inline SVG (holo base ring, gradient torso, head with two glow eyes), tinted with the node-type color; name plate shows node label + type-derived role word (开始→接待、条件/意图→导演、llm→模型师、agent→执行师、ai_agent→智能体、brain→我的 Agent、end→司仪 …).
- States: `waiting / idle / running / success / failed / skipped`, expressed by ring color, breathing/bobbing keyframes and a status glow. `prefers-reduced-motion` disables all motion (states still visible).
- Edges render as SVG bezier paths; branch labels (condition/intent) stay visible on the path.

## Choreography

- `node.started` → human switches to `running`, bubble shows the event message, ripple emits.
- `node.finished` (success) → one glowing particle per outgoing edge travels from source to target (~600 ms, `getPointAtLength` + rAF); on arrival the target switches to `waiting` glow. Failed/skipped finishes emit no particles.
- `node.log` → ticker text on the running human's bubble (truncated) and a feed entry.
- `run.started` / `run.finished` → stage banner with final status; humans settle to their final states from the run object (authoritative), so a missed event never leaves a stale state.
- Poll cadence reuses the canvas monitor's contract: `has_more` → immediate next page; terminal run and page drained → stop; errors back off exponentially (capped 10 s), 401/403/404 stop tracking.

## Live, Replay, Manual Run

- Entering the view scans `/api/runs`: newest `running` run auto-attaches (live). Otherwise the newest run loads as a finished board with a 回放 button.
- 回放 resets the stage and re-plays the run's stored events using their real relative timestamps (gaps clamped to 50–1200 ms), so historical runs animate identically to live ones.
- 运行 opens the shared inputs dialog for the selected flow and starts a background run; the stage attaches to it. The button is hidden without `resource.write` (viewers watch and replay only).

## Message Feed

Right panel lists every event as a message card (time, level color, sender label, text, traceback collapse). Feed follows the tail unless the user scrolled up; entries from/related to a human highlight that human's card; clicking a feed entry pulses the corresponding human. Capped at 400 entries.

## Failure Handling

- Run without graph snapshot → roster layout note; no particles, feed and states still work.
- Flow deleted mid-view → selector re-renders from `/api/flows`; attached run keeps reading by run_id.
- View leave / workspace switch / logout → timers and in-flight requests cancelled (generation guard, same pattern as the canvas monitor).
- Stage resize re-fits via `ResizeObserver`; animations pause on `document.hidden`.

## Files

- `web/index.html` — nav tab `nav-dh`, `#dh-view` section, asset version bumps.
- `web/app.js` — 数字人舞台 section (state, stage builder, avatar renderer, choreography, monitor, replay, feed), `switchView`/`applyPermissions` wiring.
- `web/style.css` — stage layout, figure states/keyframes, bubble, particle, feed, responsive stacking.
- `tests/test_flow_server.py` — markup assertions: the shell serves the 数字人 tab and stage skeleton.

## Verification

1. Focused pytest (markup + existing flow suite) stays green — no backend change.
2. Real browser: login → 数字人 → run the governed demo flow → observe human states, particle hops along edges, feed growth, run banner; replay a historical run; 800 px viewport stacks correctly. Screenshots under `gui-test-screenshots/`.

## Addendum (2026-09-25): Workstations, Layouts, Walking Delivery

- **Workstations**: each avatar is a person at a desk (desk + glowing monitor in front, person behind). The figure gained legs/shoes (full body); the desk hides the legs when seated at the station.
- **Layout modes** (segmented control in the side panel, persisted): `tree` (default) — office-floor arrangement: BFS order wrapped into serpentine rows (≈3 per row) with wide aisles; density beyond the stage auto-scales all stations uniformly (`--dhs`); `circle` — all stations on an ellipse in BFS order; `graph` — follow the canvas snapshot coordinates. Positions are stage-space coordinates (`dh.pos`); switching modes rebuilds positions without losing run state (`dhRelayout`).
- **Workstation identity**: each station sits on its own floor pad (role-color tinted, dashed border) so stations read as separated personal zones; the desk was raised so the person's hands rest on the desktop.
- **People v3**: arms with hands (swing while walking), radial-shaded face, eyelids + two-part lips, nostril shading, fabric folds, hair highlights, and left/right facing variety — figures no longer all face identically.
- **Walking delivery**: message passing is no longer a flying particle. When a node starts, its (successful) predecessor's person walks out from behind their desk to the recipient's workstation (leg-swing + bob animations, distance-based duration, sequential per-person queue), the recipient pulses with a 收到交付 bubble, then the sender walks back. Reduced-motion / hidden tab degrade to a pulse only.
- Realistic half-body figures (varied skin tone, hair style/color per node) were introduced earlier the same day; the walking upgrade extends them to full body.

## Addendum 2 (2026-09-25): Team Mode — Multi-Agent Collaboration on Stage

The digital workforce layer (`workforce.py` / `board.py` / `bus.py` / `crew.py` + workforce routes & policy, first built 2026-09-21 and verified on the Gitee line) was ported to this repo: draft→start gate re-checks the planned roster, bus overflow drains oldest-first, unread/mark-read share one SQL predicate, `GET /api/workforce/teams/{id}/snapshot` + SSE `/api/workforce/events`, policy section `workforce` (max_teams/members/rounds/cards, allowed_roles, auto_create_agents) with `PolicyEngine` normalization. 145 ported tests pass unchanged.

The stage gains a 团队任务 panel: a task is composed (LLM or rules fallback) into a roster (老板 + product/architect/developer/qa/ops/sales), the crew driver advances rounds (serial within a round, parallel across roles), and the stage renders every member as a digital human at their own workstation. Bus messages drive the walking delivery (human→boss figure, broadcast→first employee), employee status maps to avatar states, the banner shows round/card progress, and the boss can speak via the feed input (auto-resumes a waiting team). Stage rebuild triggers on roster change (draft→provisioned) — not only on empty stage; team attach invalidates flow polling (generation bump) and `dhRelayout` is team-aware so neither mode can clobber the other. Without an LLM key the whole loop runs on rule fallback: compose → 7 rounds → all milestone cards done → 已交付.
