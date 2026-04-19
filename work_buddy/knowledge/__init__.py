"""Unified agent knowledge system.

Two parallel hierarchies share a common base:

* **System docs** — behavioral directions, system docs, capability
  metadata, workflow structure — JSON-backed in ``knowledge/store/``.
  User patches in ``knowledge/store.local/`` (gitignored).

* **Personal knowledge** — user-authored insights, patterns, feedback,
  preferences — markdown-backed in the Obsidian vault. Queryable through
  the same search infrastructure.

Agents query via ``knowledge`` (both stores), ``agent_docs`` (system
only), or ``knowledge_personal`` (personal only).
"""

from work_buddy.knowledge.model import (
    KnowledgeUnit,
    PromptUnit,
    DirectionsUnit,
    SystemUnit,
    CapabilityUnit,
    WorkflowUnit,
    VaultUnit,
    unit_from_dict,
    validate_dag,
)
from work_buddy.knowledge.store import (
    load_store,
    get_unit,
    get_children,
    get_subtree,
    invalidate_store,
)
from work_buddy.knowledge.search import search
from work_buddy.knowledge.query import agent_docs, agent_docs_rebuild

__all__ = [
    # Model
    "KnowledgeUnit",
    "PromptUnit",
    "DirectionsUnit",
    "SystemUnit",
    "CapabilityUnit",
    "WorkflowUnit",
    "VaultUnit",
    "unit_from_dict",
    "validate_dag",
    # Store
    "load_store",
    "get_unit",
    "get_children",
    "get_subtree",
    "invalidate_store",
    # Search
    "search",
    # Query (MCP-facing)
    "agent_docs",
    "agent_docs_rebuild",
]
