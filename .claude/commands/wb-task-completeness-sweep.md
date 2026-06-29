---
short: Audit the whole task list for already-done tasks
---
Load directions via `mcp__work-buddy__wb_run("agent_docs", {"path": "tasks/completeness-sweep-directions", "depth": "full"})`, then follow them: enumerate open tasks with `task_list`, warn about cost and get a go/no-go, fan out the `task-completeness` investigator over every open task (deferring all mutations to a reviewable `AUDIT.md`), then apply backdated toggles only on the user's sign-off. Pass `$ARGUMENTS` as an optional scope hint (e.g. "older than 30 days").
