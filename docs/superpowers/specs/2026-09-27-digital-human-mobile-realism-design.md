# Flow Studio Mobile Digital Human Realism Design

## Goal

Make the Flow Studio digital-human view usable on mobile and replace the current cartoon SVG figures with locally packaged, photo-realistic digital employees. A person sits at their workstation while idle or working, stands up to deliver a message, walks to the recipient, then returns to the same workstation and sits down again.

This change remains presentation-only. Run state, dynamic-agent projection, event persistence, replay, permissions, and Flow definitions keep their current contracts.

## Scope

Included:

- Restore reliable vertical scrolling in the digital-human view on iOS and Android browsers.
- Generate six anonymous Asian office-worker identities, each with a seated-working pose and a standing/walking pose.
- Package all generated images locally; no CDN or runtime image-generation dependency.
- Keep each node bound to one stable identity across refreshes, live runs, and replay.
- Preserve workstation layout, live status, bubbles, delivery queues, reduced-motion behavior, and historical replay.

Excluded:

- Lip sync, speech synthesis, 3D models, skeletal animation, or video avatars.
- User-uploaded portraits and identity management.
- Backend, governance, or run-event schema changes.

## Visual Direction

The stage should read as a quiet operations floor rather than a character game. The surrounding Flow Studio chrome keeps its current restrained light/dark theme. The people become the single visual signature: documentary-style, photo-realistic office employees with neutral expressions, consistent camera angle, soft studio lighting, and transparent backgrounds.

The six identities vary in gender presentation, age, face shape, hairstyle, and skin tone while sharing professional contemporary clothing. Images must depict synthetic, non-famous people and must not imitate a known person.

Each identity has a matched pair:

1. `seated`: three-quarter seated working pose, hands near a keyboard, framed to sit naturally behind the existing desk and monitor.
2. `standing`: full-body or knee-up walking-ready pose in the same clothes, hairstyle, lighting, and identity, isolated on transparency.

Generated source assets are normalized to WebP with consistent canvas dimensions and visual scale. The browser never fetches a remote image.

## Asset Contract

Assets live under:

```text
src/flow_studio/web/assets/digital-humans/
  employee-01-seated.webp
  employee-01-standing.webp
  ...
  employee-06-seated.webp
  employee-06-standing.webp
```

The paired images use the same identity. The first pose establishes the identity; the standing pose is generated as an edit/variant using the seated image as the identity reference. Transparent edges, hands, hair, clothing, and feet are visually inspected before acceptance.

Final images target a 2:3 portrait canvas and are resized/compressed for the stage. The implementation should keep the full asset set small enough for a local operational UI; individual files should normally remain below 300 KB.

## Stable Identity Mapping

`dhIdentityIndex(node)` hashes the stable node identity rather than using render order:

- Flow nodes: `node.id`.
- Runtime children: `runtime:<flowNodeId>:<agentId>`.
- Workforce employees: their persisted employee ID.

The hash selects one of six image pairs. A refresh or replay therefore preserves the face. Identity selection does not encode role, status, gender, or performance.

If either pose fails to load, the figure falls back to a neutral initials portrait built from the node label. It does not fall back to the old cartoon SVG.

## Figure Structure And Pose State

Each `.dh-person` contains two image layers:

```html
<img class="dh-pose dh-pose-seated" ...>
<img class="dh-pose dh-pose-standing" ...>
```

At a workstation:

- The seated layer is visible behind the desk and monitor.
- The standing layer is transparent and non-interactive.
- Idle breathing is a restrained one- or two-pixel vertical movement; running state uses a status ring and monitor pulse, not exaggerated body bobbing.

During delivery:

1. The delivery queue selects the sender and target exactly as it does today.
2. The seated pose cross-fades out while the standing pose fades in at the sender's workstation.
3. The standing person moves along the existing delivery trajectory to the target workstation. A subtle alternating translate/rotate step effect suggests walking without deforming the photo.
4. The recipient pulses and displays the delivery bubble.
5. The sender returns along the same trajectory.
6. At the home workstation the standing pose fades out and the seated pose fades back in.

Only the standing pose moves. The workstation, desk, monitor, name plate, and home status ring remain fixed. Per-person delivery queues remain sequential so one identity cannot be in two places simultaneously.

For `prefers-reduced-motion`, background tabs, or a missing standing pose, the delivery degrades to a recipient pulse and message bubble without walking.

## Mobile Scrolling And Layout

The global application keeps `body { overflow: hidden }` because the Flow canvas depends on it. On viewports up to 860 px, `#dh-view` becomes the explicit page scroll owner:

- `flex: 1`, `min-height: 0`, and a viewport-bounded height inside the body flex column.
- `overflow-y: auto`, momentum scrolling, and `touch-action: pan-y`.
- The stage also allows `pan-y`, so dragging over empty stage space scrolls the page.
- The layout stacks controls, stage, and message feed in one column.
- The stage uses a stable responsive height of roughly 420-520 px rather than expanding to the event count.
- The message feed keeps a bounded internal height with momentum scrolling and allows scroll chaining at its boundaries.
- Safe-area bottom padding prevents the browser toolbar from covering the final controls.

Desktop keeps the current three-column, viewport-fitted layout and internal run/feed scrolling.

## Data Flow

The existing live and replay paths remain authoritative:

```text
run events / dynamicTeamProjection
              |
              v
       dhApplyEvents / dhSyncStates
              |
      +-------+--------+
      |                |
status styling   delivery choreography
      |                |
seated portrait   seated -> standing -> walk -> seated
```

Image choice is deterministic local presentation state. It is never written into the Flow, Agent, Run, or event records.

## Error Handling

- Broken seated or standing image: show initials fallback and keep all status/feed behavior.
- Standing pose unavailable during delivery: pulse the recipient; do not leave a person hidden or displaced.
- Interrupted animation or view switch: cancel movement, restore the home transform, show seated pose, and continue the existing queue safely.
- Slow image decode: preload all twelve images on first entry to the digital-human view; the stage may render fallback portraits until decoding completes.
- Asset-generation or transparency quality failure: reject that pair before code integration rather than shipping mismatched identities.

## Files

- Add: `src/flow_studio/web/assets/digital-humans/*.webp`.
- Modify: `src/flow_studio/web/app.js` for stable identity selection, paired-pose markup, preload/fallback, and delivery pose transitions.
- Modify: `src/flow_studio/web/style.css` for portrait composition, standing/walking states, and mobile scroll ownership.
- Modify: `tests/test_flow_server.py` for served asset references and mobile/pose contract assertions.
- Add focused browser acceptance evidence under the existing temporary screenshot/test-output location; do not commit temporary screenshots unless the project already tracks them.

## Verification

Automated:

- Existing Flow Studio tests remain green.
- Add assertions for paired local portrait paths, stable identity mapping hooks, initials fallback, and walking pose classes.
- Add a DOM/browser check that all twelve local assets return 200 and decode with non-zero dimensions.
- Keep JavaScript syntax and `git diff --check` clean.

Browser acceptance:

- Desktop: three-column layout, three and six identities, live state changes, replay, dark/light themes.
- Mobile at 390x844 and a tall iPhone viewport: swipe from header, empty stage space, an avatar, and the message feed; verify the view can reach every section and final control.
- Confirm no horizontal overflow, no clipped names, and no toolbar overlap.
- Run a delivery event and visually confirm: seated at home -> stands -> walks to recipient -> recipient acknowledges -> returns -> sits.
- Confirm the same person and clothing appear in both poses before accepting each identity pair.

## Acceptance Criteria

- A user can scroll the entire digital-human page on a phone even when the gesture starts on the stage.
- A message delivery visibly starts with the sender seated and ends with the sender seated at the original workstation.
- The walking figure is the same recognizable synthetic identity as the seated figure.
- Live and replay modes use identical pose transitions and never mutate persisted run data.
- Missing assets or reduced motion do not block the workflow or leave the stage in a broken pose.
