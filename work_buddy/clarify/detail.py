"""Retrieve detailed information about triage items on demand.

Provides a source-agnostic interface for agents to inspect specific
items — Haiku summaries, raw content, or both.  Used during the
review phase when the agent hits a content gap.

The summary index is written by the ``summarize`` step and persisted
as a temp file.  Raw content retrieval delegates to source adapters.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Temp file where the summarize step writes the item→summary index.
# Survives across auto_run subprocess boundaries within a single workflow run.
_INDEX_DIR = Path(tempfile.gettempdir()) / "wb_triage"
_SUMMARY_INDEX_PATH = _INDEX_DIR / "summary_index.json"
_CONTENT_INDEX_PATH = _INDEX_DIR / "content_index.json"


# ── Index I/O ───────────────────────────────────────────────────


def write_summary_index(
    items_with_summaries: list[dict[str, Any]],
    summaries: dict[str, dict[str, Any]],
) -> None:
    """Write the item→summary index after the summarize step.

    Called by ``enrich_items_with_summaries`` to persist the mapping.

    Args:
        items_with_summaries: List of TriageItem dicts (with url, id, metadata).
        summaries: {url: summary_dict} from the Haiku summarization step.
    """
    _INDEX_DIR.mkdir(parents=True, exist_ok=True)

    index: dict[str, dict[str, Any]] = {}
    for item in items_with_summaries:
        item_id = item.get("id", "")
        url = item.get("url", "")
        label = item.get("label", "")
        source = item.get("source", "")

        entry: dict[str, Any] = {
            "item_id": item_id,
            "url": url,
            "label": label,
            "source": source,
        }

        # Attach summary if available
        summary = summaries.get(url) or item.get("metadata", {}).get("summary_data")
        if summary:
            entry["summary"] = summary

        index[item_id] = entry

    _SUMMARY_INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Summary index written: %d items → %s", len(index), _SUMMARY_INDEX_PATH)


def write_content_index(content_map: dict[str, str], url_to_id: dict[str, str]) -> None:
    """Write the item→raw content index after the extract step.

    Args:
        content_map: {url: page_text} from content extraction.
        url_to_id: {url: item_id} mapping.
    """
    _INDEX_DIR.mkdir(parents=True, exist_ok=True)

    index: dict[str, str] = {}
    for url, text in content_map.items():
        item_id = url_to_id.get(url)
        if item_id:
            index[item_id] = text

    _CONTENT_INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Content index written: %d items → %s", len(index), _CONTENT_INDEX_PATH)


def _read_summary_index() -> dict[str, dict[str, Any]]:
    """Read the summary index. Returns {} if not available."""
    if not _SUMMARY_INDEX_PATH.exists():
        return {}
    try:
        return json.loads(_SUMMARY_INDEX_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Could not read summary index: %s", e)
        return {}


def _read_content_index() -> dict[str, str]:
    """Read the content index. Returns {} if not available."""
    if not _CONTENT_INDEX_PATH.exists():
        return {}
    try:
        return json.loads(_CONTENT_INDEX_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Could not read content index: %s", e)
        return {}


# ── Public API ──────────────────────────────────────────────────


def triage_item_detail(
    item_id: str,
    include_raw: bool = False,
    max_raw_chars: int = 5000,
) -> dict[str, Any]:
    """Retrieve detail for a specific triage item.

    Source-agnostic — works for Chrome tabs, journal entries, conversations,
    or any other source that has been through the triage pipeline.

    Args:
        item_id: The TriageItem ID (e.g., "tab_786de35645").
        include_raw: If True, also return raw content (page text, etc.).
            Prefer summaries unless the raw content is specifically needed.
        max_raw_chars: Max characters of raw content to return.

    Returns:
        Dict with item metadata, Haiku summary (if available), and
        optionally raw content.
    """
    index = _read_summary_index()

    entry = index.get(item_id)
    if not entry:
        return {
            "found": False,
            "item_id": item_id,
            "error": f"Item {item_id} not found in summary index. Run the triage pipeline first.",
        }

    result: dict[str, Any] = {
        "found": True,
        "item_id": item_id,
        "label": entry.get("label", ""),
        "url": entry.get("url", ""),
        "source": entry.get("source", ""),
    }

    # Summary
    summary = entry.get("summary")
    if summary:
        result["summary"] = {
            "content_summary": summary.get("content_summary", ""),
            "entities": summary.get("entities", []),
            "key_claims": summary.get("key_claims", []),
            "user_intent_speculation": summary.get("user_intent_speculation", ""),
            "user_posture": summary.get("user_posture", ""),
        }
        result["has_summary"] = True
    else:
        result["has_summary"] = False

    # Raw content
    if include_raw:
        content_index = _read_content_index()
        raw = content_index.get(item_id)
        if raw:
            result["raw_content"] = raw[:max_raw_chars]
            result["raw_truncated"] = len(raw) > max_raw_chars
        else:
            # Try live extraction for Chrome tabs
            result["raw_content"] = None
            result["raw_note"] = (
                "Raw content not in index. For Chrome tabs, use "
                "chrome_content(tab_filter=...) to extract live."
            )

    return result


# ── MCP capability wrapper ──────────────────────────────────────


def triage_item_detail_capability(
    *,
    item_id: str,
    include_raw: bool = False,
    max_raw_chars: int = 5000,
) -> str:
    """MCP capability wrapper — returns markdown."""
    result = triage_item_detail(item_id, include_raw=include_raw, max_raw_chars=max_raw_chars)

    if not result.get("found"):
        return f"Item `{item_id}` not found. {result.get('error', '')}"

    lines = [f"## {result['label']}"]
    if result.get("url"):
        lines.append(f"URL: {result['url']}")
    lines.append(f"Source: {result['source']}")
    lines.append("")

    if result.get("has_summary"):
        s = result["summary"]
        lines.append("### Summary")
        lines.append(s["content_summary"])
        if s.get("user_intent_speculation"):
            lines.append(f"\n**Intent speculation:** {s['user_intent_speculation']}")
        if s.get("user_posture"):
            lines.append(f"**User posture:** {s['user_posture']}")
        entities = s.get("entities", [])
        if entities:
            ent_str = ", ".join(f"{e['name']} ({e['type']})" for e in entities[:6])
            lines.append(f"**Entities:** {ent_str}")
        claims = s.get("key_claims", [])
        if claims:
            lines.append("**Key claims:**")
            for c in claims:
                lines.append(f"- {c}")
    else:
        lines.append("*No Haiku summary available for this item.*")

    if include_raw and result.get("raw_content"):
        lines.append("\n### Raw Content")
        lines.append(f"```\n{result['raw_content']}\n```")
        if result.get("raw_truncated"):
            lines.append(f"*(truncated to {max_raw_chars} chars)*")
    elif include_raw and result.get("raw_note"):
        lines.append(f"\n*{result['raw_note']}*")

    return "\n".join(lines)
