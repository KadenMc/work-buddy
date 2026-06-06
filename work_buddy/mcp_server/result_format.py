"""Shared markdown formatting for structured IR / vault search results.

Extracted from ``ops/context_ops.py`` so the vault-search op renders hits with the
identical look as ``context_search`` without duplicating the formatter. This module
is deliberately **outside** ``ops/`` and registers no ops — ``load_builtin_ops``
never imports/reloads it, so it is safe to import from any op module.
"""
from __future__ import annotations


def format_result_header(r: dict) -> str:
    """Render the per-result header line, dispatching on ``source``.

    Each source surfaces different identifying metadata; the conversation format
    ``[project] session`` is meaningless for a task-note or vault hit. Unknown
    sources fall back to ``[source] doc_id`` so a header always renders something.
    """
    meta = r.get("metadata", {}) or {}
    source = r.get("source", "")
    doc_id = r.get("doc_id", "")

    if source == "conversation":
        proj = meta.get("project_name") or "?"
        sid = (meta.get("session_id") or "?")[:12]
        return f"### [{proj}] {sid}"

    if source == "task_note":
        tid = meta.get("task_id") or "?"
        state = meta.get("task_state") or "?"
        return f"### [task] {tid} ({state})"

    if source == "docs":
        kind = meta.get("kind") or "doc"
        path = meta.get("path") or doc_id
        return f"### [{kind}] {path}"

    if source == "chrome":
        title = meta.get("title") or meta.get("tab_title") or "?"
        return f"### [tab] {title[:80]}"

    if source == "projects":
        slug = meta.get("slug") or doc_id
        name = meta.get("name") or slug
        return f"### [project] {name} ({slug})"

    if source == "vault_index":
        path = meta.get("source_path") or doc_id
        headings = meta.get("heading_path") or []
        crumb = f" › {headings[-1]}" if headings else ""
        return f"### [vault] {path}{crumb}"

    # Generic fallback — no source-specific knowledge needed.
    return f"### [{source or 'result'}] {doc_id[:48]}"


def format_results(results: list[dict], label: str) -> str:
    """Format structured result dicts into markdown."""
    if not results:
        return f"No results from {label}."

    lines = [f"*{len(results)} result(s) from {label}*", ""]
    for r in results:
        lines.append(format_result_header(r))
        scores = []
        if r.get("bm25_score"):
            scores.append(f"bm25={r['bm25_score']:.3f}")
        if r.get("dense_score"):
            scores.append(f"dense={r['dense_score']:.3f}")
        # Per-projection breakdown for multi-projection sources (e.g. task_note's
        # body / line). Only surfaces when present so the terse cases stay terse.
        proj_scores = r.get("projection_scores") or {}
        for key in sorted(proj_scores):
            val = proj_scores[key]
            if val:
                scores.append(f"{key}={val:.3f}")
        if r.get("recency_weight") is not None:
            scores.append(f"recency={r['recency_weight']:.2f}")
        if scores:
            lines.append(f"*Score: {r['score']:.4f} ({', '.join(scores)})*")
        else:
            lines.append(f"*Score: {r['score']:.4f}*")
        lines.append("")
        if r.get("display_text"):
            preview = r["display_text"][:300]
            lines.append(f"> {preview}")
            lines.append("")
    return "\n".join(lines)
