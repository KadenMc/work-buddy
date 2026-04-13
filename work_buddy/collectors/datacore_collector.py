"""Collect structural vault context via Datacore.

Runs a configurable list of named queries and formats the results as a
markdown summary. The query list is defined in CONTEXT_QUERIES below.

When no queries are configured, the collector is a no-op (returns empty
string). Add queries as they prove high-signal; remove them when they don't.

Requires Obsidian running with Datacore plugin initialized.
Degrades gracefully if unavailable.
"""

from __future__ import annotations

from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ── Configurable query list ────────────────────────────────────
#
# Each entry is a dict with:
#   name: str          — section heading in the output
#   description: str   — one-line explanation of what this query surfaces
#   query: str         — Datacore query string
#   fields: list[str]  — fields to include in results
#   limit: int         — max results to serialize
#   format: str        — "table" or "list" (how to render results)
#   key_field: str     — primary field to display (e.g. "$path", "$text")
#
# Start empty. Add queries here as we identify reliably high-signal ones.
# Each query runs independently; failures are logged and skipped.

CONTEXT_QUERIES: list[dict[str, Any]] = [
    # Example (commented out — add real ones when proven):
    #
    # {
    #     "name": "Contract-Tagged Tasks",
    #     "description": "Open tasks tagged with any active project tag",
    #     "query": '@task and $status = " " and #projects/my-project',
    #     "fields": ["$text", "$file", "$tags"],
    #     "limit": 20,
    #     "format": "list",
    #     "key_field": "$text",
    # },
]


def collect(cfg: dict[str, Any]) -> str:
    """Collect structural vault context by running configured queries.

    Returns empty string if CONTEXT_QUERIES is empty (no-op).
    """
    if not CONTEXT_QUERIES:
        return ""

    from work_buddy.obsidian import bridge

    try:
        available = bridge.is_available()
    except Exception:
        available = False

    if not available:
        return _unavailable_report("Obsidian bridge not reachable")

    try:
        from work_buddy.obsidian.datacore.env import check_ready

        status = check_ready()
        if not status.get("ready"):
            reason = status.get("reason", "unknown")
            return _unavailable_report(f"Datacore not ready: {reason}")
    except Exception as e:
        logger.warning("Datacore check_ready failed: %s", e)
        return _unavailable_report(f"check_ready error: {e}")

    sections: list[str] = ["# Vault Structure (Datacore)"]

    from work_buddy.obsidian.datacore.env import query as dc_query

    for q in CONTEXT_QUERIES:
        name = q.get("name", "Unnamed Query")
        description = q.get("description", "")
        query_str = q.get("query", "")
        fields = q.get("fields", [])
        limit = q.get("limit", 20)
        fmt = q.get("format", "list")
        key_field = q.get("key_field", fields[0] if fields else "$path")

        if not query_str:
            continue

        try:
            result = dc_query(query_str, fields=fields, limit=limit)
        except Exception as e:
            logger.warning("Datacore query '%s' failed: %s", name, e)
            sections.append(f"## {name}\n\n_Query failed: {e}_")
            continue

        total = result.get("total", 0)
        items = result.get("results", [])

        lines = [f"## {name}"]
        if description:
            lines.append(f"_{description}_")
        lines.append(f"\n**{total} results** (showing {len(items)})")

        if not items:
            lines.append("\n_No results._")
        elif fmt == "table" and len(fields) > 1:
            # Table format
            headers = " | ".join(f"`{f}`" for f in fields)
            sep = " | ".join("---" for _ in fields)
            lines.append(f"\n| {headers} |")
            lines.append(f"| {sep} |")
            for item in items:
                vals = " | ".join(
                    _truncate(str(item.get(f, "")), 60) for f in fields
                )
                lines.append(f"| {vals} |")
        else:
            # List format
            for item in items:
                primary = _truncate(str(item.get(key_field, "")), 100)
                extras = [
                    f"`{f}`={_truncate(str(item.get(f, '')), 40)}"
                    for f in fields
                    if f != key_field and item.get(f)
                ]
                extra_str = f" ({', '.join(extras)})" if extras else ""
                lines.append(f"- {primary}{extra_str}")

        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis."""
    text = text.strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _unavailable_report(reason: str) -> str:
    """Generate a minimal report when Datacore is not available."""
    return (
        "# Vault Structure (Datacore)\n\n"
        f"Datacore context not available: {reason}\n\n"
        "To enable: open Obsidian, ensure the Datacore plugin is "
        "installed and has finished indexing."
    )
