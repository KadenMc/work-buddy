---
name: Event Source Create
kind: capability
description: Author an event source — validate the structured fields and write <event_sources>/<name>.md. Builds a polling watcher that fetches state, reacts on a meaningful change, evaluates a CEL condition, and fires a scoped action (notify). Refuses to overwrite unless overwrite=true; a malformed source returns its validation errors.
capability_name: event_source_create
category: events
op: op.wb.event_source_create
schema_version: wb-capability/v1
parameters:
  name:
    type: str
    description: Source name (1-64 chars, alphanumeric start; becomes the .md file stem)
    required: true
  source_type:
    type: str
    description: Delivery backend — http_poll (GET a URL) or fake (test/no-network)
    required: true
  interval:
    type: str
    description: Poll interval, e.g. '30s', '5m', '6h', '1d'
    required: true
  url:
    type: str
    description: URL to GET (required for http_poll)
    required: false
  extract_mode:
    type: str
    description: How to extract the watched value — json_path, css, or hash (whole-payload)
    required: false
  extract_path:
    type: str
    description: JSONPath (e.g. $.quote.price) or CSS selector for the watched value
    required: false
  condition:
    type: str
    description: Optional CEL predicate over event.data / prev.data; the action fires only when it is true
    required: false
  action:
    type: str
    description: Action to run on a firing change (only 'notify' is supported)
    required: false
  action_params:
    type: dict
    description: Parameters for the action (e.g. notify title/body overrides)
    required: false
  allowed_actions:
    type: list
    description: Actions this source is scoped to run; the action must be in this list (defaults to [action])
    required: false
  autonomy:
    type: str
    description: notify_only (default) or auto_execute (not yet supported)
    required: false
  max_per_hour:
    type: int
    description: Optional rate-limit; more firings than this in an hour suspends the source
    required: false
  cursor_from:
    type: str
    description: now (default; first observation is a silent baseline) or all (fire on the first observation too)
    required: false
  enabled:
    type: bool
    description: Whether the source polls immediately (default true)
    required: false
  event_type:
    type: str
    description: Override the emitted event type (defaults to ai.workbuddy.source.<name>.changed)
    required: false
  overwrite:
    type: bool
    description: Replace an existing source of the same name (default false)
    required: false
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- events
- source
- create
- author
- watcher
aliases:
- create event source
- new event source
- add a watcher
- watch a source
parents:
- events
---

Author an event source by building its frontmatter from structured fields,
validating it (known type, valid interval, valid JSONPath and CEL, action in
`allowed_actions`, valid autonomy), and writing `<event_sources>/<name>.md`. The
poller re-loads the directory each tick, so a new source activates on the next
poll — no restart needed.

This is the activate step behind `/wb-event-new`; prefer that conversational
loop (it elicits the watch, proposes a grounded draft, and dry-runs it before
calling this) over hand-filling the parameters. The action is scoped: only
actions in `allowed_actions` may run, and only `notify` exists today
(`autonomy: notify_only`).
