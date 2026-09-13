---
name: Activity Timeline
kind: skill
description: Authority-aware activity inference over native Journal SQLite records, with pre-seal Markdown compatibility and optional deeper signals.
category: journal
op: op.wb.activity_timeline
schema_version: wb-skill/v1
parameters:
  since:
    type: str
    description: ISO datetime or relative shorthand (e.g. '2h', '1d', '30m')
    required: true
  until:
    type: str
    description: 'ISO datetime. Default: now.'
    required: false
  deep:
    type: bool
    description: 'Also collect git/chat/vault signals (default: false)'
    required: false
  target_date:
    type: str
    description: 'Journal date YYYY-MM-DD (default: inferred from since)'
    required: false
skill_name: activity_timeline
tags:
- journal
- activity
- timeline
aliases:
- what happened recently
- recent activity
- activity timeline
- what have I been doing
- infer activity
- activity digest
- journal entries structured
parents:
- journal
---

Shallow mode projects visible `record` and `log` items from the native Journal
database for each requested local day. It preserves the public timeline shape,
local wall-clock ordering, tags, incomplete markers, and human/agent authorship
labels without returning Source contents or storage identifiers.

Before the durable Journal authority seal, and only while Obsidian remains an
enabled dependency, the same skill can read the legacy Log section through
`JournalContentAdapter`. `database_only` and `recovery_fenced` authority never
fall back to Markdown. A paused cutover returns no Journal projection until one
authority is established, so mutable compatibility files cannot leak back into
the post-cutover timeline.
