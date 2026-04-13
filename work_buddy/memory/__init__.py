"""Hindsight memory integration for work-buddy.

Provides a persistent personal memory layer backed by a local Hindsight
server.  Two integration surfaces share one backend:

* **Claude Code plugin** (hooks) — ambient auto-recall/retain during chat.
* **Python adapter** (this package) — programmatic retain/recall/reflect
  for context collection, workflows, and MCP gateway capabilities.

Public API
----------
>>> from work_buddy.memory import recall_personal_context, retain_personal_note, reflect_on_query
"""

from work_buddy.memory.client import get_client, health_check
from work_buddy.memory.ingest import (
    retain_context_bundle_summary,
    retain_journal_insights,
    retain_personal_note,
    retain_project_observation,
    retain_project_state_file,
    retain_workflow_outcome,
)
from work_buddy.memory.query import (
    get_mental_model,
    get_project_mental_model,
    list_recent_memories,
    memory_read,
    project_memory_read,
    prune_memories,
    recall_for_workflow,
    recall_personal_context,
    recall_project_context,
    reflect_on_query,
)
from work_buddy.memory.setup import ensure_bank, ensure_project_bank, refresh_mental_models, refresh_project_mental_models

__all__ = [
    "get_client",
    "health_check",
    # Personal memory — setup
    "ensure_bank",
    "refresh_mental_models",
    # Project memory — setup
    "ensure_project_bank",
    "refresh_project_mental_models",
    # Personal memory — ingest
    "retain_context_bundle_summary",
    "retain_journal_insights",
    "retain_personal_note",
    "retain_workflow_outcome",
    # Project memory — ingest
    "retain_project_observation",
    "retain_project_state_file",
    # Personal memory — query
    "memory_read",
    "prune_memories",
    "recall_personal_context",
    "recall_for_workflow",
    "reflect_on_query",
    "get_mental_model",
    "list_recent_memories",
    # Project memory — query
    "project_memory_read",
    "recall_project_context",
    "get_project_mental_model",
]
