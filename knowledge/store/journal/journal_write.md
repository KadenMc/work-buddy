---
name: Journal Write
kind: capability
description: 'Append log entries or persist a briefing to the journal. For log entries: pass time/description tuples. For briefing: pass markdown to wrap in a callout.'
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
    description: 'For briefing mode: markdown string'
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
requires:
- obsidian
---
