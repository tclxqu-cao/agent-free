# Homepage HTML Block Alias Compatibility

## Context

Customer Agent can complete a homepage run while returning an HTML block as
`{"type":"html","text":"..."}`. The canonical `PortfolioArtifactV1` field is
`html`, so Flow Studio rejects the otherwise valid response and the browser
shows its local unavailable card.

## Design

Flow Studio remains the Portfolio-specific contract boundary. When validating
an HTML block, it reads canonical `html` first and accepts `text` only as a
legacy model-output alias. The selected value passes through the existing size
and active-content checks, then the returned artifact is normalized to `html`
with the alias removed. Unsafe HTML remains rejected.

The Customer Agent `homepage-content-render` Skill adds an exact HTML block
example and states that HTML blocks use `html`, never `text`. The generic Flow
HTTP API stays unchanged because it is not Portfolio-specific.

## Verification

- A safe `type=html` block carrying `text` is returned as canonical `html`.
- The alias is removed from the normalized artifact.
- Unsafe markup supplied through the alias is still rejected.
- Existing homepage flow tests remain green.
