---
name: Hot Files
kind: capability
description: Rank vault files by activity intensity, fusing modification frequency (vault events) with writing intensity (Keep the Rhythm). Hierarchically collapses busy directories to prevent context flooding. Use sub_directory to drill into a specific area.
capability_name: hot_files
category: journal
op: op.wb.hot_files
schema_version: wb-capability/v1
parameters:
  since:
    type: str
    description: Relative shorthand ('7d', '2h') or ISO date ('2026-04-01')
    required: true
  sub_directory:
    type: str
    description: Vault-relative path to drill into (e.g. 'repos/work-buddy'). Shows file-level detail.
    required: false
  collapse_threshold:
    type: int
    description: Max files per directory before collapsing (default 5)
    required: false
tags:
- journal
- hot
- files
aliases:
- hot files
- most edited files
- active files
- what files changed
- recently modified
- frequently edited
- vault activity
parents:
- journal
requires:
- obsidian
---
