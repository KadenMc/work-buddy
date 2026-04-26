---
short: Search task notes via hybrid retrieval (BM25 + dense, RRF-fused)
---
Load directions via `mcp__work-buddy__wb_run("agent_docs", {"path": "tasks/task-search-directions", "depth": "full"})`, then call `context_search` with `source="task_note"` and `$ARGUMENTS` as the query.
