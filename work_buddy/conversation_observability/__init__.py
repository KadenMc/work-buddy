"""Conversation observability — durable activity signals from Claude Code sessions.

Centralizes session-derived facts that previously lived ad hoc in
``work_buddy/sessions/inspector.py``:

* **Session commits** — git commits attributed to a Claude Code session
  by parsing Bash ``git commit`` tool calls.
* **Session file writes** — Write/Edit/NotebookEdit tool calls with
  per-file latest timestamp.
* **Uncommitted work** — the intersection of recent writes and current
  ``git status --porcelain``, attributed to the last session that wrote
  each dirty file.
* **Observed sessions** — metadata snapshots (mtime, span count,
  start/end times, tool usage) for stale-only refresh.
* **Topic summaries** — bounded LLM-generated tldr + topic ranges,
  carrying model + prompt/schema version provenance for stale detection.

Why centralize: the inspector module accumulated five orthogonal
responsibilities (raw browsing, span mapping, commit extraction, write
extraction, uncommitted attribution) and was effectively a private
cache for git context source's session annotation. A dedicated
subsystem turns the cache into a queryable substrate that the journal,
context bundle, and dashboard can consume independently.

Git stays git-owned: ``work_buddy/context/sources/git.py`` continues to
own commit/status collection. Only the session-attribution layer
migrates here.

Importing this package registers the ``conversation-observability``
artifact so it appears in ``artifact_registry_dump``.
"""

from __future__ import annotations

from work_buddy.conversation_observability.artifacts import (
    register_conversation_observability_artifact,
)


register_conversation_observability_artifact()
