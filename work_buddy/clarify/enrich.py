"""Per-candidate IR context enrichment for triage pipelines.

Attaches hybrid-IR search results (keyword + semantic) to each
:class:`TriageItem` before it is handed to a reasoning agent. This
reduces the pressure on local models to make the right exploratory
tool calls — supporting material is pre-staged in
``metadata["ir_context"]`` and can simply be cited.

Source-agnostic. Usable by Chrome, journal, conversations, or any
future adapter that yields TriageItems.
"""

from __future__ import annotations

from typing import Any, Iterable

from work_buddy.logging_config import get_logger
from work_buddy.clarify.items import TriageItem

logger = get_logger(__name__)


def enrich_with_ir_context(
    items: list[TriageItem],
    *,
    top_k: int = 5,
    source: str | None = None,
    method: str = "keyword,semantic",
    max_text_chars: int = 600,
) -> list[TriageItem]:
    """Attach IR context hits to each item under ``metadata["ir_context"]``.

    Failures are tolerated per-item — a single bad search doesn't
    abort the whole batch. On failure, the item is returned with
    ``metadata["ir_context"] = []`` and ``metadata["ir_error"]`` set.

    Args:
        items: TriageItems to enrich. Mutated in place AND returned.
        top_k: IR hits per item. Small — this is a pre-brief, not
            a full retrieval pass.
        source: Optionally restrict search to one IR source
            (e.g. ``"conversation"``, ``"document"``). Default: all.
        method: IR method string (see ``work_buddy.ir.search``).
        max_text_chars: Truncate each item's query text to this many
            characters before hitting IR, to cap embedding cost for
            pathologically long candidates.

    Returns:
        The same list of TriageItems, each with ``metadata["ir_context"]``
        populated.
    """
    # Import is deferred so this module stays cheap to import in the
    # registry path. ir.search pulls in sqlite3 and http clients.
    from work_buddy.ir.search import search as ir_search

    for item in items:
        query = (item.text or item.label or "").strip()
        if not query:
            item.metadata["ir_context"] = []
            continue
        if len(query) > max_text_chars:
            query = query[:max_text_chars]

        try:
            hits = ir_search(
                query,
                top_k=top_k,
                source=source,
                method=method,
            )
        except Exception as exc:
            logger.warning(
                "IR enrichment failed for item %s: %s", item.id, exc,
            )
            item.metadata["ir_context"] = []
            item.metadata["ir_error"] = f"{type(exc).__name__}: {exc}"
            continue

        # ir.search returns a list of hits on success or a string on
        # input error. Treat the string case as "no results, remember why."
        if isinstance(hits, str):
            logger.info(
                "IR enrichment returned error string for item %s: %s",
                item.id, hits,
            )
            item.metadata["ir_context"] = []
            item.metadata["ir_error"] = hits
            continue

        item.metadata["ir_context"] = [
            _shape_hit(h) for h in hits
        ]

    return items


def render_ir_context(
    hits: Iterable[dict[str, Any]],
    *,
    max_hits: int = 5,
    max_chars_per_hit: int = 400,
) -> str:
    """Format IR hits as a compact text block for an LLM prompt.

    Produces something like::

        [ir] conversations/2026-04-15T14-22_agent:
          - "…matching snippet…"
        [ir] projects/work-buddy:
          - "…"

    The shape is optimized for local-model token economy: source path
    as a tag, a short quoted display_text, one per hit.
    """
    lines: list[str] = []
    for hit in list(hits)[:max_hits]:
        src = hit.get("source", "")
        doc = hit.get("doc_id", "")
        disp = (hit.get("display_text") or "").strip().replace("\n", " ")
        if len(disp) > max_chars_per_hit:
            disp = disp[: max_chars_per_hit - 1] + "…"
        tag = f"{src}/{doc}" if src and doc else (src or doc or "hit")
        lines.append(f"[ir] {tag}:\n  - {disp!r}")
    return "\n".join(lines)


def _shape_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Trim an IR hit to the fields we want to persist alongside the item.

    Drops per-method sub-scores and any heavy raw payloads we don't
    want sitting in every pool entry.
    """
    return {
        "doc_id": hit.get("doc_id", ""),
        "source": hit.get("source", ""),
        "score": round(float(hit.get("score", 0.0)), 4),
        "display_text": hit.get("display_text", ""),
        "metadata": hit.get("metadata", {}),
    }
