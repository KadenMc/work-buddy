---
schedule: "0 0,12 * * *"  # midnight + noon local
recurring: true
jitter_seconds: 300  # up to 5 min, avoid hammering the same instant on every tick
type: capability
capability: claude_code_usage_scan
params: {}
enabled: true
---

# Claude Code usage scan

Twice-daily incremental scan of Claude Code's transcript JSONLs into the cost
cache at `<data_root>/cache/claude_code_usage.db`. Keeps the Costs tab's
"Daily token volume" chart current without requiring a user to click the
toolbar Refresh button.

Incremental by file mtime (see `parse_jsonl_file` and the per-file
`processed_files` row in `work_buddy.llm.claude_code_usage.scanner`), so a
missed tick is self-healing — the next run picks up everything written
since the last successful ingest.
