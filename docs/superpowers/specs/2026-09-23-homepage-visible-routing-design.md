# Homepage Visible Routing Design

## Goal

Make the published `homepage-main` graph express public command routing with visible Flow Studio node types. Exact Slash commands must bypass AI classification, while natural-language input must use the existing intent node. Condition branches must be configurable as safe value comparisons without requiring users to hand-write an entire expression.

## Routing Architecture

The graph starts with one condition node whose source is `input.message`. Each non-default outgoing edge compares that value with one exact Slash command. Matching commands enter their fixed Portfolio Content Agent Skill branch. The `else` edge enters one intent node configured with top-level and per-project natural-language intents; that node directly selects the same fixed Skill branches.

The old `homepage.route` action and the second `switch` condition are removed. `/job` remains an alias of `/jobs`. The jobs branch still reads the real read-only job snapshot before invoking `portfolio-jobs`. The public entry point, Agent allowlist, artifact validation, cache, and security boundaries do not change.

## Condition Configuration

A condition node gains `source`, a safe namespace path such as `input.message`, `route.intent`, or `match.total`. Each outgoing edge may use either:

- structured comparison fields: `operator`, `value`, and `value_type`; or
- the existing raw `branch` expression for backward compatibility.

Supported structured operators are `equals`, `not_equals`, `contains`, `not_contains`, `greater_than`, `greater_or_equal`, `less_than`, and `less_or_equal`. `else` remains the fallback edge. Values are typed as `string`, `number`, `boolean`, or `null`.

The engine resolves the source path from the Flow namespace and evaluates comparisons directly. It does not execute Python, JavaScript, function calls, imports, or arbitrary code. Existing raw expressions continue through the restricted AST evaluator.

The condition inspector displays the source path and an outgoing-branch editor with target, operator, type, and comparison value. The edge inspector presents the same structured fields and an advanced raw-expression option.

## Intent Configuration

The existing intent node keeps its current contract: a source utterance plus configured names, descriptions, and samples. It returns `intent`, `confidence`, and `via`, then chooses the matching outgoing edge itself. No extra condition node follows it.

Project aliases become intent samples and route to explicit project Skill nodes. This keeps every project branch visible and removes the dynamic `{{route.skill}}` dispatch.

## Compatibility And Migration

Existing flows with expression-only condition edges continue to run and edit unchanged. Structured edge fields are additive. The governed built-in homepage revision changes so Flow Studio creates and publishes a new `homepage-main` version without replacing unrelated user drafts.

Homepage graph validation continues to allow only the restricted node and Skill set. The removed `route` action may remain registered for compatibility with historical flow versions but is no longer present in the published graph.

## Failure Handling

An invalid structured condition is treated as a non-match and evaluation continues to the next edge, matching existing invalid-expression behavior. If no rule matches, the first `else` edge is selected. Intent misses enter the help branch. Skill failures and artifact fallback retain their current behavior.

## Verification

Focused tests cover structured comparisons and raw-expression compatibility, graph topology, Slash commands bypassing intent classification, natural-language routing, explicit project branches, jobs snapshot context, governed migration, and condition editor markup. The final verification runs the relevant Flow Studio test suite and checks the live graph/API without restarting the existing services unless required to load the changed code.
