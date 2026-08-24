---
name: Task Archive Directions
kind: directions
description: How to archive completed tasks — map the user's scope intent to older_than_days, default to a 7-day buffer, and let the consent prompt be the scope check
summary: Sets the native archived lifecycle on eligible completed tasks. Default scope leaves the last 7 days visible; no Markdown is moved or rendered.
trigger: user runs /wb-task-archive or asks to archive, clean up, or tidy completed tasks
command: wb-task-archive
capabilities:
- tasks/task_archive
tags:
- tasks
- archive
- cleanup
- directions
aliases:
- archive done tasks
- clean up completed tasks
- move completed to archive
- tidy task list
- archive old tasks
parents:
- tasks
---

Archive via mcp__work-buddy__wb_run("task_archive", {"older_than_days": <int>}). Do NOT use Python code.

`task_archive` only archives **completed** native tasks. It sets structured
lifecycle state in `TaskStore`; it never moves Markdown and has no Obsidian
dependency.

## Choosing older_than_days

Default to **7** — the capability's recently-done buffer. Override that default when the user's intended scope is clear, reading both the argument and the surrounding conversation:

- `all` / `everything` / "every completed task" → **0** (archive every completed task regardless of age)
- a bare number, "N days", or "last N days" → **N**
- relative phrases — "last month" ≈ **30**, "two weeks" ≈ **14**, "last week" ≈ **7**
- **contextual override**: if the user already stated a scope earlier in the conversation (e.g. "archive all tasks"), honor that over the bare 7-day default even when the launcher is invoked with no argument.

When the signal is genuinely ambiguous, stay on the 7-day default — the consent prompt is the backstop.

## Consent

The call prompts the user with an exact count and a random 5-title sample so they approve a concrete scope. Do NOT pre-confirm on the user's behalf or ask your own "are you sure?" first — issue the call and let its prompt be the scope check.

## Presentation

Report concisely how many native tasks were archived. A summary notification is posted automatically.

## Do NOT

- Do not try to archive incomplete tasks — the capability only moves completed ones.
- Do not list every archived task.
- Do not recommend process changes or a different cadence; just do the archive the user asked for.
