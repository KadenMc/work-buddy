---
name: Context Git
kind: skill
description: 'Recent git activity across all repos: commits, diffs, dirty trees. Pass annotate=true to tag commits made by agent sessions with their session ID.'
category: context
op: op.wb.context_git
schema_version: wb-skill/v1
parameters:
  days:
    type: int
    description: Lookback window for commit history (default 7)
    required: false
  dirty_only:
    type: bool
    description: Only repos with uncommitted changes (default false)
    required: false
  annotate:
    type: bool
    description: Tag commits made by agent sessions with session ID (default false). Slower — scans JSONL files.
    required: false
param_aliases:
  since: days
skill_name: context_git
tags:
- context
- git
aliases:
- what changed in git
- recent commits
- repo status
- code changes
- git diff
- which session made this commit
- agent commits
parents:
- context
---
