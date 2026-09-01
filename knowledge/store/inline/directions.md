---
name: Inline Commands Directions (Retired)
kind: directions
description: Route new selection and action features to native application surfaces rather than the retired Obsidian inline framework.
summary: 'Do not add Obsidian menu handlers or #wb/cmd tags. Implement native selection/action affordances with domain APIs, provenance, consent, and idempotency as required.'
trigger: user asks to add an action formerly modeled as an Obsidian inline command
tags:
- inline
- directions
- retired
- native-actions
aliases:
- add inline command
- register inline
parents:
- inline
---

Do not extend `work_buddy/inline/` or add another `#wb/cmd/*` scanner. Place the
action on the owning native surface and call its domain API. Preserve the
domain's consent, CAS/idempotency, privacy, and provenance rules. If a legacy
file must be inspected for migration, use a bounded operator path; do not turn
the importer into a recurring product feature.
