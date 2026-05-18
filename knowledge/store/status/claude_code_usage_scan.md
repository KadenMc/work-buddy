---
name: Claude Code Usage Scan
kind: capability
description: Scan Claude Code's local transcript JSONLs into the cost cache (~/.claude/projects/**/*.jsonl). Incremental by default. Use full_rebuild=true after a pricing or schema change.
capability_name: claude_code_usage_scan
category: llm
parameters:
  full_rebuild:
    type: bool
    description: Drop and rebuild the cache (default false).
    required: false
mutates_state: true
retry_policy: manual
tags:
- llm
- claude
- code
- usage
- scan
aliases:
- claude usage
- claude code usage
- transcript scan
- ingest claude code activity
- rescan costs
parents:
- status
- status
---
