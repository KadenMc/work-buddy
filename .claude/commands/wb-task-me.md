---
short: What should I do right now?
workflow: task-me
---
Load directions via `mcp__work-buddy__wb_run("agent_docs", {"path": "tasks/task-me-directions", "depth": "full"})`, then run the workflow via `mcp__work-buddy__wb_run("task-me")`.

Optional first argument is a context preset (`at_desk`, `phone_only`, `untethered`); pass it through as the `user_current_contexts` input on the load-context step.
