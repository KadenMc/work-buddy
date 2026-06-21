---
name: Event Source Dry Run
kind: capability
description: Preview an event source without any side effects — fetch, diff against the last value, and evaluate the condition, but never publish, run an action, or advance the cursor. Returns the sampled value, whether it changed, the would-emit event, and whether the condition would pass. The preview the /wb-event-new authoring loop shows before activating.
capability_name: event_source_dry_run
category: events
op: op.wb.event_source_dry_run
schema_version: wb-capability/v1
parameters:
  name:
    type: str
    description: The event source name (the .md file stem). Provide this to preview a saved source.
    required: false
  proposal:
    type: dict
    description: The structured event_source_create fields (name, source_type, interval, extract_*, condition, action, ...), to preview a not-yet-saved source — what /wb-event-new's dry-run step passes. Provide either name or proposal.
    required: false
mutates_state: false
retry_policy: none
is_action: false
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- events
- source
- dry-run
- preview
aliases:
- preview event source
- dry run source
parents:
- events
---

Run a single source poll with **zero side effects**: fetch the payload, extract
the watched value, diff it against the stored cursor, and — if it changed —
evaluate the source's CEL condition over the would-emit event. Nothing is
published, no action runs, and the cursor is not advanced, so this is safe to
call repeatedly while authoring. Returns `{changed, current, prev, would_emit,
condition_passed, would_fire}` so the author can see exactly what *would* happen
before activating the source.
