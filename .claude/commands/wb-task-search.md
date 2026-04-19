Search task-note bodies with hybrid retrieval (BM25 + dense vectors, RRF-fused).

Covers every task whose `note_uuid` is non-null and whose markdown file
exists at `<vault_root>/tasks/notes/<uuid>.md`. Indexed every 5 minutes
by the `task-note-index` sidecar job.

Call:

```
mcp__work-buddy__wb_run("context_search", {
    "query": "$ARGUMENTS",
    "source": "task_note",
    "top_k": 10
})
```

Present results with the task_id and task_state from each hit's metadata
so the user can jump to the task. For very short queries (<=3 tokens),
prefer `method="keyword"` to reduce dense-model noise.
