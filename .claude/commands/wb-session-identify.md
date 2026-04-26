---
short: Locate a prior Claude Code conversation by topic and drill into its key turns
---
Load directions via `mcp__work-buddy__wb_run("agent_docs", {"path": "context/session-identify", "depth": "full"})`, then follow the procedure with `$ARGUMENTS` as the user's handoff (topic, time window, keywords, known false positives).
