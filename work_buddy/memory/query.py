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
        and entity names for best results (e.g. named work-pattern vocabulary
        rather than a generic label like "blindspots"). Ignored for model/recent.
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

    If ``project_slug`` is provided, scopes to that project — and to
    every alias attached to it via ``project_aliases``. A memory tagged
    with any of {canonical_slug, *aliases} matches.

    If omitted, searches across all projects.
    """
    _ensure_project_bank_once()

    # Build the set of project tags to query against. If a project_slug
    # is provided, resolve via the store and union with its aliases so
    # memories tagged with any prior slug for the same project still
    # surface alongside memories tagged with the current canonical slug.
    slug_set: list[str] = []
    if project_slug:
        slug_set.append(project_slug)
        try:
            from work_buddy.projects import store
            pid = store.resolve_slug(project_slug)
            if pid is not None:
                row = store.get_project_by_id(pid, include_deleted=True)
                if row:
                    if row["slug"] not in slug_set:
                        slug_set.append(row["slug"])
                    for a in row.get("aliases", []):
                        norm = a.get("alias_norm")
                        if norm and norm not in slug_set:
                            slug_set.append(norm)
        except Exception:
            logger.debug(
                "Could not resolve aliases for project %r; querying base slug only",
                project_slug,
            )

    if slug_set:
        tag_parts = [f"project:{s}" for s in slug_set]
        tags = build_tags(*tag_parts)
        # Any of these project tags matching is enough. ``any_strict``
        # also unions any non-project tags that ``build_tags`` adds —
        # acceptable because the union still scopes within the project
        # bank.
        tags_match = "any_strict"
    else:
        tags = build_tags()
        tags_match = "any_strict"

    client = get_client()
    try:
        resp = _run_hindsight_recall_in_thread(
            client,
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
        scope = (
            f"project:{project_slug} (aliases: {len(slug_set) - 1})"
            if project_slug else "cross-project"
        )
        logger.info("Recalled %d chars for %s: %.60s…", len(text), scope, query)
        return text
    except Exception:
        logger.exception("Project recall failed for: %.60s…", query)
        return ""


def _run_hindsight_in_thread(method_name: str, **kwargs):
    """Run a Hindsight call in a fresh thread with its own asyncio loop
    **and** its own Hindsight client instance.

    Two things conspire to make the cached sync client unsafe when
    invoked from a Flask request worker:

    1. The cached ``Hindsight`` client lazily creates an
       ``aiohttp.ClientSession`` on first use. The session is bound
       to whichever event loop ran that first call.
    2. The client's sync methods call ``loop.run_until_complete`` on
       the calling thread's loop. If the session's loop and the
       current loop disagree, aiohttp's timeout-context manager
       fires ``RuntimeError: Timeout context manager should be used
       inside a task``.

    A fresh client created inside a fresh thread starts a fresh
    aiohttp session in that thread's loop — both pieces match, no
    cross-loop confusion. ``method_name`` is the attribute name on
    the Hindsight client to invoke; for async variants (e.g.
    ``arecall``) the coroutine is driven on the fresh loop. The
    thread is created per-call (these endpoints aren't on hot paths)
    so we don't pay the cost of a long-lived executor or session
    pool.
    """
    import asyncio
    import threading
    from work_buddy.memory.client import _cfg
    from hindsight_client import Hindsight

    result = [None]
    error = [None]

    def _runner():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            cfg = _cfg()
            local_client = Hindsight(
                base_url=cfg.get("base_url", "http://localhost:8888"),
            )
            method = getattr(local_client, method_name)
            r = method(**kwargs)
            if asyncio.iscoroutine(r):
                r = loop.run_until_complete(r)
            result[0] = r
        except Exception as e:
            error[0] = e
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()

    if error[0] is not None:
        raise error[0]
    return result[0]


# Back-compat alias for the original recall-specific signature.
def _run_hindsight_recall_in_thread(_unused_client, **kwargs):
    return _run_hindsight_in_thread("arecall", **kwargs)


def recall_project_context_items(
    query: str,
    *,
    project_slug: str | None = None,
    budget: str = "mid",
    max_tokens: int = 4096,
) -> list[dict[str, Any]]:
    """Same recall as :func:`recall_project_context` but returns
    structured items instead of a text dump.

    Use this when a caller needs to render each result individually
    (e.g. a UI log) rather than feed the raw text into an LLM prompt.
    Alias resolution + tag union are identical to the text variant.

    Returns a list of ``{id, type, text, tags}`` dicts in Hindsight's
    relevance ordering. Returns ``[]`` on any failure (Hindsight
    unavailable, project unknown, etc.).
    """
    _ensure_project_bank_once()

    slug_set: list[str] = []
    if project_slug:
        slug_set.append(project_slug)
        try:
            from work_buddy.projects import store
            pid = store.resolve_slug(project_slug)
            if pid is not None:
                row = store.get_project_by_id(pid, include_deleted=True)
                if row:
                    if row["slug"] not in slug_set:
                        slug_set.append(row["slug"])
                    for a in row.get("aliases", []):
                        norm = a.get("alias_norm")
                        if norm and norm not in slug_set:
                            slug_set.append(norm)
        except Exception:
            logger.debug(
                "Could not resolve aliases for project %r; querying base slug only",
                project_slug,
            )

    if slug_set:
        tag_parts = [f"project:{s}" for s in slug_set]
        tags = build_tags(*tag_parts)
        tags_match = "any_strict"
    else:
        tags = build_tags()
        tags_match = "any_strict"

    client = get_client()
    try:
        resp = _run_hindsight_recall_in_thread(
            client,
            bank_id=get_project_bank_id(),
            query=query,
            budget=budget,
            types=["world", "experience", "observation"],
            tags=tags,
            tags_match=tags_match,
            max_tokens=max_tokens,
            include_entities=False,  # we don't render entities in the items UI
        )
    except Exception:
        logger.exception("Project recall (items) failed for: %.60s…", query)
        return []

    raw_results = getattr(resp, "results", None) or []
    items: list[dict[str, Any]] = []
    for r in raw_results:
        if isinstance(r, dict):
            items.append({
                "id": r.get("id", ""),
                "type": r.get("type", ""),
                "text": r.get("text", ""),
                "tags": list(r.get("tags", [])),
            })
        else:
            items.append({
                "id": getattr(r, "id", ""),
                "type": getattr(r, "type", ""),
                "text": getattr(r, "text", ""),
                "tags": list(getattr(r, "tags", []) or []),
            })
    logger.info(
        "Recalled %d items for %s: %.60s…",
        len(items),
        f"project:{project_slug} (aliases: {max(0, len(slug_set) - 1)})"
        if project_slug else "cross-project",
        query,
    )
    return items


def list_recent_project_memories(
    *,
    project: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Chronological list of Hindsight memories scoped to a project.

    No embedding query — Hindsight returns memories in reverse-chrono
    order via ``list_memories``. We page through and filter
    client-side against the alias-union tag set so memories tagged
    with a prior slug (e.g. ``project:electricrag`` for a row now
    slugged ``ecg-inquiry``) still surface after a rename.

    Returns ``[{id, type, text, tags, date}]`` newest first. ``type``
    is taken from Hindsight's ``fact_type`` field. Returns ``[]`` on
    any failure (Hindsight unavailable, project unknown, etc.).

    Use this — *not* :func:`recall_project_context_items` — when the
    caller wants a chronological log of a project's memories. Recall
    is for relevance-ranked retrieval into an LLM prompt; listing is
    free of embedding cost and the right primitive for UIs that just
    show "what's been recorded here."
    """
    _ensure_project_bank_once()

    # Alias-union tag set
    tag_forms: set[str] = {f"project:{project}"}
    try:
        from work_buddy.projects import store
        pid = store.resolve_slug(project)
        if pid is not None:
            row = store.get_project_by_id(pid, include_deleted=True)
            if row:
                tag_forms.add(f"project:{row['slug']}")
                for a in row.get("aliases", []):
                    norm = a.get("alias_norm")
                    if norm:
                        tag_forms.add(f"project:{norm}")
    except Exception:
        logger.debug(
            "Could not resolve aliases for %r; filtering by base slug only",
            project,
        )

    def _matches(m: Any) -> bool:
        tags = (
            m.get("tags", []) if isinstance(m, dict)
            else getattr(m, "tags", []) or []
        )
        return any(t in tag_forms for t in tags)

    def _to_item(m: Any) -> dict[str, Any]:
        d = dict(m) if isinstance(m, dict) else {
            "id": getattr(m, "id", ""),
            "fact_type": getattr(m, "fact_type", ""),
            "text": getattr(m, "text", ""),
            "date": getattr(m, "date", ""),
            "tags": getattr(m, "tags", []),
        }
        return {
            "id": d.get("id", ""),
            "type": d.get("fact_type", "") or d.get("type", ""),
            "text": d.get("text", ""),
            "date": d.get("date", ""),
            "tags": list(d.get("tags", []) or []),
        }

    try:
        # Three pages × 200 = up to 600 items scanned. Each page is
        # one round-trip through a freshly-instantiated Hindsight
        # client (see `_run_hindsight_in_thread`), so widening the
        # window past this point makes the endpoint visibly slower
        # without a corresponding payoff for the active projects.
        # Quiet projects with no state-file ingest yet will return
        # empty regardless of pagination — that's the correct answer.
        page_size = 200
        max_pages = 3
        matched: list[Any] = []
        for page in range(max_pages):
            resp = _run_hindsight_in_thread(
                "list_memories",
                bank_id=get_project_bank_id(),
                limit=page_size,
                offset=page * page_size,
            )
            items = (
                getattr(resp, "items", None)
                or getattr(resp, "memories", [])
                or []
            )
            if not items:
                break
            matched.extend(m for m in items if _matches(m))
            if len(matched) >= limit:
                break
        matched = matched[:limit]
        out = [_to_item(m) for m in matched]
        logger.info(
            "Listed %d project memories (project=%s, alias tags=%d)",
            len(out), project, len(tag_forms),
        )
        return out
    except Exception:
        logger.exception("Failed to list project memories for %s", project)
        return []


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
