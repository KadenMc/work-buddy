---
name: Task Search Directions
kind: directions
description: How to search tasks - distinguish description-text search (task_search, store-only) from note-body hybrid retrieval (context_search source=task_note)
trigger: user runs /wb-task-search or asks to search task-note bodies for content matching a query
command: wb-task-search
capabilities:
- task_search
- context_search
tags:
- tasks
- search
- retrieval
- hybrid
- bm25
- dense
- directions
aliases:
- search tasks
- find task
- search task notes
- task search
- hybrid task search
parents:
- tasks
---

There are TWO ways to search tasks. Pick by what you want to find.

Note: to *list* tasks (the whole filtered set) rather than search by text, use `task_list`. `task_search` with an empty query intentionally returns nothing.

## task_search - search by description text (Slice 3, store-only)

Looks at the `description` column of `task_metadata` - the human-readable task line text (`Fix the auth bug`, `Refactor the dashboard`, ...). Bridge-independent: works even when Obsidian isn't running. Cheap, deterministic, no embedding service.

```
mcp__work-buddy__wb_run("task_search", {"query": "$ARGUMENTS"})
```

Use this when:
  - The user asked "do I have a task about X?" and X is a phrase from the task description, not the note body.
  - You need to look up a task ID from a description.
  - The bridge is down and you still need to find a task.

Returns task records (full task_metadata rows) ordered most-recently-updated first. Description column was added in Slice 3 - pre-Slice-3 tasks may have NULL description and won't surface here until task_sync runs (<=30 min).

## context_search(source="task_note") - search by note body (hybrid)

Hybrid retrieval (BM25 + dense vectors, RRF-fused) over the linked task-note BODIES (the `[[uuid|✍]]`-linked detail files). Doesn't see tasks without notes (the majority of lightweight tasks).

```
mcp__work-buddy__wb_run("context_search", {
    "query": "$ARGUMENTS",
    "source": "task_note",
    "top_k": 10
})
```

Use this when:
  - The user is looking for a task by its note content (decisions, context, subtask details).
  - You want semantic / fuzzy matches, not exact-substring.
  - The query is natural-language and longer than a few words.

## Method choice on context_search

The default method (`keyword,semantic`, RRF fused) is right for most natural-language queries.

For very short queries (<=3 tokens), prefer `method="keyword"`: the dense model adds noise on tiny inputs.

For exact-string lookups (a UUID, a tag, a code snippet), prefer `method="substring"` - no embedding service required.

## Presentation

For each hit, surface:
1. The matching span (description for task_search; chunk text for context_search).
2. The `task_id` and `task_state` so the user can jump to the task.
3. Score / rank so multiple competing hits are comparable.

If zero hits, say so explicitly. If zero hits on `task_search`, consider falling back to `context_search(source="task_note")` - the task may have a note that mentions the query even if the description doesn't.

## Do NOT

- Don't use task_search when the user wants semantic search - it's exact-substring only.
- Don't use context_search(source="task_note") when the bridge is down - task_search is the bridge-independent path.
- Don't interpret a hit as a task assignment. Search returns context only. To claim a task, follow up with `task_assign` (or `/wb-task-assign <task_id>`).
