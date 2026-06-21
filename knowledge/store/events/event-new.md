---
name: Event New
kind: workflow
description: Author an event source in conversation — elicit what to watch, propose a grounded EventSourceDef draft, dry-run it with zero side effects, confirm, then activate it. The conversational front end for event_source_create; builds polling watchers (e.g. a stock-watcher) without hand-editing .md files.
workflow_name: event-new
execution: main
allow_override: false
steps:
- id: elicit
  name: Understand what to watch, what change matters, and the reaction
  step_type: reasoning
  depends_on: []
  result_schema:
    required_keys:
    - watch
    - change
    - reaction
    key_types:
      watch: str
      change: str
      reaction: str
  invokes: []
- id: propose
  name: Draft a grounded EventSourceDef proposal
  step_type: reasoning
  depends_on:
  - elicit
  result_schema:
    required_keys:
    - name
    - proposal
    key_types:
      name: str
      proposal: dict
  invokes: []
- id: dry_run
  name: Preview the proposed source with zero side effects
  step_type: code
  depends_on:
  - propose
  visibility:
    mode: summary
    include_keys:
    - ok
    - current
    - changed
    - would_fire
    - condition_passed
    - error
  invokes:
  - event_source_dry_run
- id: confirm
  name: Show the draft + preview; get explicit confirmation
  step_type: reasoning
  depends_on:
  - dry_run
  result_schema:
    required_keys:
    - confirmed
    key_types:
      confirmed: bool
  invokes: []
- id: activate
  name: Write the validated source
  step_type: code
  depends_on:
  - confirm
  visibility:
    mode: summary
    include_keys:
    - success
    - file_path
    - error
  invokes:
  - event_source_create
- id: monitor
  name: Surface the first firings and how to pause
  step_type: reasoning
  depends_on:
  - activate
  invokes: []
command: wb-event-new
tags:
- events
- source
- authoring
- workflow
- watcher
aliases:
- new event source
- author a watcher
- watch a source
- create event source workflow
parents:
- events
dev_notes: |-
  `dry_run` and `activate` are code steps that `invoke` events ops. The proposal
  produced by `propose` is the structured `event_source_create` parameter set
  (name + source_type + interval + extract + condition + action + ...); both
  `event_source_dry_run(proposal=...)` and `event_source_create(**proposal)`
  consume the same shape, so there is one draft object end-to-end. The dry-run
  runs against an *unsaved* proposal (no file is written until `activate`), so
  the confirm gate genuinely precedes any write.
---

Author an event source in conversation. The loop elicits what to watch, proposes
a grounded draft, previews it with zero side effects, confirms, and only then
writes the source — mirroring `/wb-dev-document`'s propose→confirm→apply gate.

## Philosophy

An event source is a small, durable watcher: it polls some state, reacts only on
a *meaningful change* (a diff, not every fetch), evaluates a condition, and fires
a scoped action. Hand-authoring the `.md` is error-prone; this loop keeps the
draft grounded to what actually exists (registered source types and actions) and
shows a real preview before anything is written or fires.

## Grounding (propose only what exists)

- **source.type**: `http_poll` (GET a URL). `fake` is test-only — don't propose it.
- **extract.mode**: `json_path` (e.g. `$.quote.price`), `css`, or `hash` (whole-payload).
- **action**: `notify` only. **autonomy**: `notify_only` only. No action that changes
  state runs — a watcher notifies; the user acts.
- **condition**: a CEL predicate over `event.data` / `prev.data` (and the `current`
  shorthand). The action fires only when it is true. `abs()` is available for
  threshold conditions.

Never invent a source type, action, or autonomy the registry doesn't have. If the
user wants something outside this set, say so plainly and stop — don't fabricate a
source that will fail validation.

## elicit

Reasoning step. Draw out three things in plain language:
- **watch** — what state to watch and where (a URL + the field, e.g. "NVDA's price
  and CEO from this JSON quote endpoint").
- **change** — what change is worth a notification (e.g. "the CEO changes, or the
  price moves more than 5%").
- **reaction** — what should happen (a notification — confirm the user wants
  notify-only).

Advance with `{"watch": "...", "change": "...", "reaction": "..."}`.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## propose

Reasoning step. Turn the elicited intent into a structured draft — the exact
parameter set `event_source_create` takes. Translate "change" into a CEL condition
grounded to the extracted fields. Example draft for the stock-watcher:

```json
{
  "name": "nvda-watch",
  "proposal": {
    "name": "nvda-watch",
    "source_type": "http_poll",
    "url": "https://example.test/quote/NVDA.json",
    "interval": "6h",
    "extract_mode": "json_path",
    "extract_path": "$",
    "condition": "event.data.ceo != prev.data.ceo || abs(event.data.price - prev.data.price) / prev.data.price > 0.05",
    "action": "notify",
    "allowed_actions": ["notify"],
    "autonomy": "notify_only"
  }
}
```

Notes:
- `name` must be 1–64 chars, alphanumeric-start (it becomes the `.md` stem).
- Use `extract_path: "$"` (whole object) when the condition reads multiple fields
  (`event.data.ceo`, `event.data.price`); use a scalar path (`$.price`) plus
  `event.data != prev.data` when watching a single value.
- Keep `action` inside `allowed_actions` — the source is scoped to exactly the
  actions you list.

Advance with `{"name": "<name>", "proposal": { ...the full param set... }}`.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## dry_run

Code step. Preview the proposed source with **zero side effects** — no file is
written, nothing is published, no action runs:

```
mcp__work-buddy__wb_run("event_source_dry_run", {"proposal": <propose.proposal>})
```

The result reports `current` (the sampled value), `changed`, the `would_emit`
event, `condition_passed`, and `would_fire`. The first observation is a silent
baseline — `would_fire` is normally `false` on a brand-new source (it fires on the
*next* change), so read this step as "did the fetch + extraction + condition
*compile and produce a sane value*," not "will it fire right now." If `ok` is
`false`, surface the validation error and loop back to `propose` to fix the draft.

Advance with the dry-run result.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## confirm

Reasoning step. Show the user the draft in plain language plus the dry-run sample
(the value it read, and the condition it will fire on). Ask for an explicit yes.

If the user wants changes, advance with `{"confirmed": false}` and loop back to
`propose` with their edits. Only advance with `{"confirmed": true}` on a clear yes.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## activate

Code step. Only run this on `confirm.confirmed == true`. Write the source by
calling `event_source_create` with the confirmed proposal:

```
mcp__work-buddy__wb_run("event_source_create", <propose.proposal>)
```

The op re-validates and writes `<event_sources>/<name>.md`. The poll tick re-loads
sources each run, so the watcher activates on the next poll — no restart. If the
result is `{"success": false}`, surface the errors and loop back to `propose`.

Advance with the create result.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## monitor

Reasoning step. Confirm the source is live, tell the user its poll interval and
what it will notify on, and remind them they can pause it any time:

```
mcp__work-buddy__wb_run("event_source_toggle", {"name": "<name>", "enabled": false})
```

Surface the first firing when it lands (it arrives as a normal notification). Wrap
up the workflow.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.
