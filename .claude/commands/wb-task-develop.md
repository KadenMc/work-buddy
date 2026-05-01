---
short: Develop a task into ordered action items at pickup
workflow: task-develop
---
Load directions via `mcp__work-buddy__wb_run("agent_docs", {"path": "tasks/task-develop-directions", "depth": "full"})`, then run the workflow via `mcp__work-buddy__wb_run("task-develop", {"task_id": "<id>"})`.

Task ID is required as the first argument. The engage flow auto-invokes this when `compute_pickup_readiness(task)` returns `ready=False`; manual use is for explicit pre-development of a specific task.

The hallucination gate is load-bearing: only items the user explicitly accepts during the review step get persisted with `approved_at` set. Rejected items are discarded.
