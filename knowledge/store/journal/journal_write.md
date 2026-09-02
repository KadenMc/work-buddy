---
name: Journal Write
kind: capability
description: 'Create Source-backed native Journal records or a generated briefing artifact, with AI provenance and no automatic Truth analysis.'
capability_name: journal_write
category: journal
op: op.wb.journal_write
schema_version: wb-capability/v1
parameters:
  mode:
    type: str
    description: '''log_entries'' (default) or ''briefing'''
    required: false
  target:
    type: str
    description: 'Date target: ''today'', ''yesterday'', or YYYY-MM-DD'
    required: false
  entries:
    type: str
    description: 'For log_entries: JSON list of [time, description] tuples'
    required: false
  briefing_md:
    type: str
    description: 'For briefing mode: exact generated artifact text.'
    required: false
  client_mutation_id:
    type: str
    description: Stable idempotency key. Reuse it only when retrying the exact same write.
    required: false
mutates_state: true
retry_policy: verify_first
consent_operations:
- update_journal_entry
- morning.persist_briefing
param_aliases:
  target_date: target
  date: target
tags:
- journal
- write
aliases:
- write journal
- append log
- journal entry
- persist briefing
- update log
parents:
- journal
requires: []
---

Each complete rendered record or briefing is committed to Sources before the
Journal row is created. The Journal entry retains the Source dependency,
attributed agent run, `ai` authorship, and `unreviewed` review state. The
default interaction behavior is provenance-only; writing does not enable Truth
or run analysis. The active profile chooses the destination module, and no
Markdown file or Obsidian projection is written.
