# Mobile Realistic Digital Humans Implementation Plan

> **For the main agent:** Implement this plan directly in the current session. Do not dispatch implementation or code-review subagents. After all development tasks are complete, run the affected unit tests and fix any failures before reporting completion. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Flow Studio digital-human page scroll correctly on phones and render stable photo-realistic employees who sit at workstations, stand to deliver messages, walk to recipients, return, and sit again.

**Architecture:** Twelve local WebP assets form six stable identity pairs (`seated` and `standing`). The existing event and delivery queue selects identities deterministically, switches to the standing pose before motion, and moves only the standing layer; a narrow FastAPI route serves only the approved asset filenames. Mobile keeps the application body's canvas constraint and makes `#dh-view` the explicit touch-scroll owner.

**Tech Stack:** Python 3.12, FastAPI `FileResponse`, vanilla JavaScript, CSS, Pillow, bundled imagegen CLI with `gpt-image-2`, pytest, ego-browser.

## Global Constraints

- Preserve all existing Flow, Run, dynamic-team, replay, workforce, permissions, and governance data contracts.
- Use six synthetic non-famous Asian office-worker identities, each with matched seated and standing poses.
- Store final images locally under `src/flow_studio/web/assets/digital-humans/`; do not use a CDN or runtime generation.
- Map stable node IDs to identities; never infer identity from role, status, gender, or performance.
- A delivery must visibly follow `seated -> standing -> walk -> return -> seated` and restore safely on interruption.
- Mobile scrolling must work when the gesture begins on the header, stage, avatar, or message feed.
- Desktop keeps its three-column viewport-fitted layout.
- Respect `prefers-reduced-motion` and provide an initials fallback for broken assets.
- Preserve all unrelated dirty-worktree changes.

---

### Task 1: Generate And Normalize Identity Assets

**Files:**
- Create: `output/imagegen/digital-humans/*.png`
- Create: `src/flow_studio/web/assets/digital-humans/employee-01-seated.webp` through `employee-06-standing.webp`

**Interfaces:**
- Consumes: the bundled `image_gen.py` CLI and the approved photo-realistic paired-pose prompt contract.
- Produces: twelve WebP files named `employee-NN-{seated|standing}.webp`, each with alpha and consistent 2:3 framing.

- [x] **Step 1: Verify the CLI environment without exposing credentials**

Resolve the active CC Switch Codex provider without printing its credential and verify `openai` plus `Pillow` in the project virtual environment. Use `/Users/caoqu/.codex/skills/.system/imagegen/scripts/image_gen.py`; do not modify it or create an SDK wrapper.

- [x] **Step 2: Generate six distinct seated identities**

For each identity, run CLI `generate` with `gpt-image-2`, `1024x1536`, medium or high quality, use case `photorealistic-natural`, a plain near-white seamless background, and this invariant prompt structure:

```text
Photorealistic synthetic Asian office employee, non-famous and not based on a real person.
Three-quarter seated working pose, hands naturally near a keyboard, contemporary professional clothing,
neutral attentive expression, soft studio light, consistent eye-level camera, full person silhouette visible.
Pure near-white seamless background, no desk, no monitor, no text, no logo, no watermark, no extra people.
```

Vary age, gender presentation, face shape, hair, skin tone, and clothing color across the six jobs while keeping the same camera and light.

- [x] **Step 3: Generate each matching standing pose by edit**

Use the seated source as the identity reference for CLI `edit` and require:

```text
Preserve exactly the same synthetic identity, face, hair, clothing, lighting, and camera.
Change only the pose to a natural walking-ready standing pose with both legs visible and arms relaxed.
Pure near-white seamless background, no furniture, no text, no logo, no watermark, no extra people.
```

- [x] **Step 4: Remove the connected near-white background and normalize WebP output**

Use Pillow only for deterministic post-processing: flood from border-connected near-white pixels, feather antialiased edges, crop to non-transparent bounds, center on a consistent 1024x1536 transparent canvas, downscale to web dimensions, and save WebP with alpha. Reject a pair if dimensions are zero, transparency is absent, or seated/standing identity and clothing do not visually match.

### Task 2: Serve Only Approved Portrait Assets

**Files:**
- Modify: `src/flow_studio/server.py:630-650`
- Unit tests: `tests/test_flow_server.py`

**Interfaces:**
- Consumes: filenames matching `employee-(01..06)-(seated|standing).webp`.
- Produces: `GET /assets/digital-humans/{filename}` with `image/webp` and immutable cache headers; invalid names return `404`.

- [x] **Step 1: Add a fixed asset directory and filename validator**

Define the directory relative to `WEB_DIR` and validate filenames with an anchored regular expression. Reject traversal and all non-WebP or out-of-range names before resolving a path.

- [x] **Step 2: Add the static portrait route**

Return `FileResponse` with `media_type="image/webp"` and `Cache-Control: public, max-age=31536000, immutable`. Return the existing structured 404 behavior for invalid or missing approved files.

- [x] **Step 3: Add route tests**

Assert one valid image returns `200`, WebP content type, non-empty bytes, and immutable caching. Assert traversal, invalid pose, and out-of-range identity return `404`.

### Task 3: Stable Paired-Pose Avatar Renderer

**Files:**
- Modify: `src/flow_studio/web/app.js:2587-2713`
- Unit tests: `tests/test_flow_server.py`

**Interfaces:**
- Consumes: a node with stable `id` and `label`, plus twelve portrait URLs.
- Produces: `dhIdentityIndex(node)`, `dhPortraitUrl(index, pose)`, `dhInitials(label)`, and paired `.dh-pose-seated` / `.dh-pose-standing` markup.

- [x] **Step 1: Replace render-order identity selection with a stable hash**

Implement a deterministic unsigned string hash over `node.id` and map it into `[0, 5]`. Keep the result stable across rebuilds and replay.

- [x] **Step 2: Render both local poses and initials fallback**

Replace `dhFigureSvg()` usage with two lazy-decoded images and a fallback element. Attach load/error handling in `dhPlaceAvatar()` so successful decode marks each pose ready and any failure exposes initials without breaking status interactions.

- [x] **Step 3: Preload all portrait pairs on digital-human entry**

Create `dhPreloadPortraits()` that instantiates all twelve images once and invoke it from `dhEnter()`. Preloading failure remains non-fatal.

- [x] **Step 4: Update static contract tests**

Assert stable identity and portrait helper names, seated/standing class names, fallback class, local asset prefix, and preload call are served in `app.js`.

### Task 4: Seated-To-Standing Delivery State Machine

**Files:**
- Modify: `src/flow_studio/web/app.js:2919-2971`
- Modify: `src/flow_studio/web/style.css:965-1051`
- Unit tests: `tests/test_flow_server.py`

**Interfaces:**
- Consumes: the existing sequential `dhWalks.chain`, source/target workstation offsets, and paired pose readiness.
- Produces: `.is-standing` and `.is-walking` transitions with guaranteed `dhRestoreHomePose(fromId)` cleanup.

- [x] **Step 1: Add explicit pose helpers**

Implement helpers that switch a figure to standing only when the standing image is ready, restore transforms and transition durations, and always end in seated state.

- [x] **Step 2: Update `dhWalkOnce()` choreography**

Switch seated to standing, wait for the short stand transition, move the person to the target, acknowledge, return, then restore the seated pose. Reduced motion, hidden documents, missing standing assets, node removal, view changes, and exceptions pulse the recipient and restore the sender. The pose switch is intentionally deterministic because throttled mobile/browser timelines can leave opacity transitions at frame zero while movement has already started.

- [x] **Step 3: Replace cartoon limb animation styles**

Remove dependencies on `.dh-leg`, `.dh-arm`, `.dh-eyes`, `dhFigureSvg`, and cartoon-specific keyframes. Add restrained photo motion: a small vertical step/bob on the standing image, deterministic pose switch, status ring, seated breathing, success/error treatment, and no layout shift.

- [x] **Step 4: Add choreography contract tests**

Assert the pose helpers, standing class, guaranteed cleanup path, paired images, and removal of cartoon SVG dependencies.

### Task 5: Mobile Scroll Ownership And Responsive Stage

**Files:**
- Modify: `src/flow_studio/web/style.css:1081-1090`
- Unit tests: `tests/test_flow_server.py`

**Interfaces:**
- Consumes: the current body flex column, two-row mobile navigation, and stacked digital-human grid.
- Produces: a bounded `#dh-view` mobile scroll container with stage/feed touch scroll chaining.

- [x] **Step 1: Make `#dh-view` the mobile vertical scroll owner**

At `max-width: 860px`, use `flex: 1`, `height: auto`, `min-height: 0`, `overflow-y: auto`, `overscroll-behavior-y: contain`, `touch-action: pan-y`, and `-webkit-overflow-scrolling: touch`. Add safe-area bottom padding.

- [x] **Step 2: Stabilize stacked section heights**

Use grid rows `auto clamp(420px, 56dvh, 520px) auto`; make the stage and avatar layers allow `pan-y`; keep the run list bounded; give the message feed a bounded mobile height with momentum scrolling and scroll chaining.

- [x] **Step 3: Add responsive contract tests**

Assert the mobile media block includes scroll ownership, momentum scroll, `touch-action: pan-y`, stage clamp, and safe-area padding while the desktop grid remains unchanged.

- [x] **Step 4: Forward header-originated mobile gestures**

When the digital-human view is active at mobile width, forward vertical wheel and touch gestures that begin on the fixed global navigation to `#dh-view`. Preserve horizontal navigation gestures and ordinary taps, and keep the proxy disabled in every other view.

### Task 6: Cache Version, Browser Acceptance, And Runtime Delivery

**Files:**
- Modify: `src/flow_studio/web/index.html:13,252`
- Modify: `tests/test_flow_server.py`

**Interfaces:**
- Consumes: completed assets, JavaScript, CSS, and the existing LaunchAgent-managed `:8788` service.
- Produces: fresh asset URLs in the served page and verified desktop/mobile runtime behavior.

- [x] **Step 1: Bump static cache query versions**

Increment both `style.css?v=` and `app.js?v=` so iOS does not retain the previous scroll and avatar implementation.

- [x] **Step 2: Run source and unit verification**

Run JavaScript syntax checking, focused Flow Studio tests, complete related Flow tests, image decode/dimension checks, and `git diff --check`. Fix failures before browser testing.

- [x] **Step 3: Restart only the Flow Studio LaunchAgent**

Identify the exact launchd label and restart only the `:8788` service. Confirm the new stable PID, health endpoint, cache-versioned HTML, portrait route, and current Flow assets.

- [x] **Step 4: Verify desktop and mobile in ego-browser**

Use one TaskSpace. Validate desktop three-column rendering and mobile viewports including `390x844`: programmatically swipe from header, empty stage, avatar, and feed; verify `scrollTop` increases, no horizontal overflow, all twelve assets decode, names do not overlap, and the last input/control remains reachable.

- [x] **Step 5: Verify standing delivery in the rendered page**

Use historical replay or a bounded synthetic page-side trigger without mutating stored Run data. Confirm the same identity starts seated, stands and moves, returns, and ends seated. Ego Browser's screenshot and direct `Page.captureScreenshot` calls timed out on this page, so acceptance used live computed pose, class, transform, bubble, and identity checks; the twelve-asset contact sheet was inspected separately.

- [x] **Step 6: Verify a large real dynamic-team history**

Attach formal Run `f3c90074d95d`, drain event pages until `has_more=false`, and verify the terminal snapshot wins over earlier progress events. The 14,217-event history loads all three temporary experts as completed while the feed retains only the latest 400 DOM cards.

## Final Unit Test Verification

- [x] **Main agent: run affected unit tests after development is complete**

Run:

```bash
uv run pytest -q tests/test_flow_server.py tests/test_flow_live_runs.py tests/test_flow_engine.py tests/test_flow_external_agent.py tests/test_flow_platform.py
```

Expected: all tests pass. If a test fails, fix the implementation or test and rerun this command until it passes. Report the command and result in the final response.
