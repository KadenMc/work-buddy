"""Recall and reflect helpers — querying the personal memory bank.

Recall is cheap (embedding + keyword search, no LLM call).
Reflect is more expensive (LLM-powered reasoning over recalled facts).
Use recall for most workflows; reserve reflect for synthesis tasks.
"""

from __future__ import annotations

from typing import Any

from work_buddy.consent import requires_consent
from work_buddy.memory.client import build_tags, get_bank_id, get_project_bank_id, get_client
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Lazy project bank bootstrap (shared flag with ingest module)
# ---------------------------------------------------------------------------

_PROJECT_BANK_ENSURED = False


def _ensure_project_bank_once() -> None:
    """Bootstrap the Hindsight project bank exactly once per process.

    Idempotent — ensure_project_bank handles "already exists" gracefully.
    Failures are logged as warnings and do not abort the calling operation.
    """
    global _PROJECT_BANK_ENSURED
    if _PROJECT_BANK_ENSURED:
        return
    try:
        from work_buddy.memory.setup import ensure_project_bank
        ensure_project_bank()
        _PROJECT_BANK_ENSURED = True
    except Exception:
        logger.warning("Failed to ensure project bank", exc_info=True)


def memory_read(
    query: str = "",
    *,
    mode: str = "search",
    model_id: str = "self-profile",
    limit: int = 20,
    budget: str = "low",
) -> str | list | dict | None:
    """Unified memory read — search, browse, or fetch a mental model.

    Parameters
    ----------
    query : str
        Descriptive topic phrase for search mode. Use specific terminology
        and entity names for best results (e.g. "scope fusion and branch
        explosion patterns" rather than "blindspots"). Ignored for model/recent.
    mode : str
        "search" (default) — semantic + keyword recall, no LLM cost.
        "model" — fetch a pre-computed mental model by model_id.
        "recent" — list recent memories (limit controls count).
    model_id : str
        Mental model to fetch when mode="model". One of: self-profile,
        work-patterns, blindspots, preferences, current-constraints.
    limit : int
        Max memories for mode="recent" (default 20).
    budget : str
        Retrieval depth for mode="search": low (fast), mid, high (thorough).
    """
    if mode == "model":
        return get_mental_model(model_id)
    if mode == "recent":
        return list_recent_memories(limit=limit)
    return recall_personal_context(query=query, budget=budget)


def recall_personal_context(
    query: str,
    *,
    budget: str = "mid",
    types: list[str] | None = None,
    extra_tags: list[str] | None = None,
    max_tokens: int = 4096,
) -> str:
    """Recall relevant personal memories for a query.

    Returns the recall text (empty string on failure).
    """
    tags = build_tags(*(extra_tags or []))
    client = get_client()
    try:
        resp = client.recall(
            bank_id=get_bank_id(),
            query=query,
            budget=budget,
            types=types or ["world", "experience", "observation"],
            tags=tags,
            tags_match="any_strict",
            max_tokens=max_tokens,
            include_entities=True,
        )
        text = getattr(resp, "text", None) or str(resp)
        logger.info("Recalled %d chars for query: %.60s…", len(text), query)
        return text
    except Exception:
        logger.exception("Recall failed for query: %.60s…", query)
        return ""


def recall_for_workflow(
    workflow_name: str,
    step_context: str,
    *,
    budget: str = "mid",
) -> str:
    """Recall memories relevant to a specific workflow step."""
    tags = build_tags(f"workflow:{workflow_name}")
    client = get_client()
    try:
        resp = client.recall(
            bank_id=get_bank_id(),
            query=step_context,
            budget=budget,
            types=["world", "experience", "observation"],
            tags=tags,
            tags_match="any_strict",
            max_tokens=4096,
            include_entities=True,
        )
        text = getattr(resp, "text", None) or str(resp)
        logger.info(
            "Recalled %d chars for workflow '%s': %.60s…",
            len(text), workflow_name, step_context,
        )
        return text
    except Exception:
        logger.exception("Recall failed for workflow '%s'", workflow_name)
        return ""


@requires_consent(
    operation="memory_reflect",
    reason="Triggers a server-side LLM call against your Anthropic API key. "
           "Each call costs tokens (~1-3K). Use memory_read for free retrieval.",
    risk="moderate",
    default_ttl=15,
)
def reflect_on_query(
    query: str,
    *,
    budget: str = "low",
    extra_tags: list[str] | None = None,
) -> str:
    """LLM-powered reasoning over accumulated memories.

    More expensive than recall (triggers an LLM call server-side).
    Returns the reflect response text.
    """
    tags = build_tags(*(extra_tags or []))
    client = get_client()
    try:
        resp = client.reflect(
            bank_id=get_bank_id(),
            query=query,
            budget=budget,
            tags=tags,
            tags_match="any_strict",
        )
        text = getattr(resp, "text", None) or str(resp)
        logger.info("Reflected %d chars for query: %.60s…", len(text), query)
        return text
    except Exception:
        logger.exception("Reflect failed for query: %.60s…", query)
        return ""


def prune_memories(
    *,
    document_id: str | None = None,
    memory_type: str | None = None,
) -> str:
    """Delete memories from the bank, or list documents for review.

    With no args: lists documents (read-only, no consent needed).
    With document_id or memory_type: deletes (consent-gated, irreversible).

    Parameters
    ----------
    document_id : str, optional
        Delete a specific document and its derived memories.
    memory_type : str, optional
        Delete all memories of a given type: "world", "experience",
        or "observation". Use when bulk-pruning a category of noise.
    """
    import json
    import urllib.request

    from work_buddy.consent import _cache as consent_cache, ConsentRequired
    from work_buddy.memory.client import _cfg

    cfg = _cfg()
    base_url = cfg.get("base_url", "http://localhost:8888")
    bank_id = get_bank_id()

    def _api_get(path: str) -> dict:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as resp:
            return json.loads(resp.read())

    def _api_delete(path: str, params: str = "") -> dict:
        url = f"{base_url}{path}"
        if params:
            url += f"?{params}"
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    # No args → list documents with memory breakdown (read-only, no consent)
    if not document_id and not memory_type:
        docs = _api_get(
            f"/v1/default/banks/{bank_id}/documents?limit=50"
        )
        doc_items = docs.get("items", [])

        # Fetch all memories to map them to documents
        all_mems = []
        mem_offset = 0
        while True:
            resp = _api_get(
                f"/v1/default/banks/{bank_id}/memories/list"
                f"?limit=50&offset={mem_offset}"
            )
            mem_items = resp.get("items", [])
            if not mem_items:
                break
            all_mems.extend(mem_items)
            mem_offset += len(mem_items)
            if mem_offset >= resp.get("total", 0):
                break

        # Build doc_id → memories mapping via chunk_id
        doc_memories: dict[str, list] = {}
        orphan_count = 0
        for m in all_mems:
            chunk_id = m.get("chunk_id", "")
            matched = False
            if chunk_id:
                for d in doc_items:
                    did = d.get("id", "")
                    if did and did in chunk_id:
                        doc_memories.setdefault(did, []).append(m)
                        matched = True
                        break
            if not matched:
                orphan_count += 1

        lines = [f"Documents in bank ({len(doc_items)} total, "
                 f"{len(all_mems)} memories, {orphan_count} orphaned):"]
        for d in doc_items:
            did = d.get("id", "?")
            created = d.get("created_at", "?")[:19]
            tags = d.get("tags", [])
            mems = doc_memories.get(did, [])
            lines.append(f"\n  [{created}] {did}")
            lines.append(f"    tags={tags}  memories={len(mems)}")
            # Show up to 3 sample memory texts
            for m in mems[:3]:
                ft = m.get("fact_type", "?")
                text = m.get("text", "")[:120]
                lines.append(f"    [{ft}] {text}")
            if len(mems) > 3:
                lines.append(f"    ... and {len(mems) - 3} more")
        if orphan_count:
            lines.append(f"\n  {orphan_count} orphan memories "
                         f"(observations, not tied to a document)")
        return "\n".join(lines)

    # Destructive operations require consent
    if not consent_cache.is_granted("memory_prune"):
        from work_buddy.agent_session import get_session_audit_path
        raise ConsentRequired(
            operation="memory_prune",
            reason="Permanently deletes memories from the Hindsight bank. "
                   "This cannot be undone.",
            risk="high",
            default_ttl=5,
        )

    # Delete by document
    if document_id:
        result = _api_delete(
            f"/v1/default/banks/{bank_id}/documents/{document_id}"
        )
        logger.info("Deleted document %s", document_id)
        return f"Deleted document {document_id}: {result}"

    # Delete by memory type
    if memory_type:
        result = _api_delete(
            f"/v1/default/banks/{bank_id}/memories",
            params=f"type={memory_type}",
        )
        logger.info("Deleted all '%s' memories", memory_type)
        return f"Deleted all '{memory_type}' memories: {result}"

    return "No action taken."


def get_mental_model(model_id: str) -> dict[str, Any] | None:
    """Retrieve a pre-computed mental model by ID."""
    client = get_client()
    try:
        resp = client.get_mental_model(bank_id=get_bank_id(), mental_model_id=model_id)
        logger.info("Retrieved mental model '%s'", model_id)
        return resp
    except Exception:
        logger.exception("Failed to get mental model '%s'", model_id)
        return None


def list_recent_memories(
    *,
    limit: int = 20,
    search_query: str | None = None,
    memory_type: str | None = None,
) -> list[Any]:
    """List recent memories with optional search and type filtering.

    Parameters
    ----------
    limit : int
        Max memories to return.
    search_query : str, optional
        Text filter for memory content.
    memory_type : str, optional
        Filter by memory type (e.g. "world", "experience", "observation").
    """
    client = get_client()
    try:
        resp = client.list_memories(
            bank_id=get_bank_id(),
            limit=limit,
            search_query=search_query,
            type=memory_type,
        )
        items = getattr(resp, "items", None) or getattr(resp, "memories", []) or []
        logger.info("Listed %d memories", len(items))
        return items
    except Exception:
        logger.exception("Failed to list memories")
        return []


# ═══════════════════════════════════════════════════════════════════
# Project memory bank
# ═══════════════════════════════════════════════════════════════════

def project_memory_read(
    query: str = "",
    *,
    mode: str = "search",
    model_id: str = "project-landscape",
    project: str | None = None,
    limit: int = 20,
    budget: str = "mid",
) -> str | list | dict | None:
    """Unified project memory read — search, browse, or fetch a mental model.

    Parameters
    ----------
    query : str
        Descriptive topic phrase for search mode.
    mode : str
        "search" (default) — semantic + keyword recall, no LLM cost.
        "model" — fetch a project mental model by model_id.
        "recent" — list recent project memories.
    model_id : str
        Mental model to fetch when mode="model". One of: project-landscape,
        active-risks, recent-decisions, inter-project-deps.
    project : str, optional
        Project slug to scope search to. Omit for cross-project.
    limit : int
        Max memories for mode="recent".
    budget : str
        Retrieval depth: low (fast), mid, high (thorough).
    """
    _ensure_project_bank_once()
    if mode == "model":
        return get_project_mental_model(model_id)
    if mode == "recent":
        return list_recent_project_memories(limit=limit, project=project)
    return recall_project_context(query=query, project_slug=project, budget=budget)


def recall_project_context(
    query: str,
    *,
    project_slug: str | None = None,
    budget: str = "mid",
    max_tokens: int = 4096,
) -> str:
    """Recall relevant project memories for a query.

    If project_slug is provided, scopes to that project.
    If omitted, searches across all projects.
    """
    _ensure_project_bank_once()
    tag_parts: list[str] = []
    if project_slug:
        tag_parts.append(f"project:{project_slug}")
    tags = build_tags(*tag_parts)

    # Scoped to one project = must match all tags; cross-project = match any
    tags_match = "all_strict" if project_slug else "any_strict"

    client = get_client()
    try:
        resp = client.recall(
            bank_id=get_project_bank_id(),
            query=query,
            budget=budget,
            types=["world", "experience", "observation"],
            tags=tags,
            tags_match=tags_match,
            max_tokens=max_tokens,
            include_entities=True,
        )
        text = getattr(resp, "text", None) or str(resp)
        scope = f"project:{project_slug}" if project_slug else "cross-project"
        logger.info("Recalled %d chars for %s: %.60s…", len(text), scope, query)
        return text
    except Exception:
        logger.exception("Project recall failed for: %.60s…", query)
        return ""


def get_project_mental_model(model_id: str = "project-landscape") -> dict[str, Any] | None:
    """Retrieve a pre-computed project mental model by ID."""
    client = get_client()
    try:
        resp = client.get_mental_model(
            bank_id=get_project_bank_id(), mental_model_id=model_id,
        )
        logger.info("Retrieved project mental model '%s'", model_id)
        return resp
    except Exception:
        logger.exception("Failed to get project mental model '%s'", model_id)
        return None


def list_recent_project_memories(
    *,
    limit: int = 20,
    project: str | None = None,
) -> list[Any]:
    """List recent project memories with optional project scoping.

    When *project* is provided, fetches a broader set and filters
    client-side by the ``project:<slug>`` tag (Hindsight's list_memories
    only supports text search, not tag filtering).
    """
    client = get_client()
    try:
        # Fetch more than requested when filtering, to ensure we get enough matches
        fetch_limit = limit * 3 if project else limit
        resp = client.list_memories(
            bank_id=get_project_bank_id(),
            limit=fetch_limit,
        )
        items = getattr(resp, "items", None) or getattr(resp, "memories", []) or []

        if project:
            tag = f"project:{project}"
            items = [m for m in items if tag in (
                m.get("tags", []) if isinstance(m, dict)
                else getattr(m, "tags", [])
            )][:limit]

        logger.info("Listed %d project memories%s", len(items),
                     f" (filtered to {project})" if project else "")
        return items
    except Exception:
        logger.exception("Failed to list project memories")
        return []
