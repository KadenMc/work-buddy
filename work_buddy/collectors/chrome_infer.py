"""Haiku-powered Chrome browsing activity inference.

Selectively reads page content from high-engagement tabs, builds
DataItems, and classifies them against intent hypotheses using the
general-purpose ``work_buddy.llm.classify`` primitive.

Chrome-specific responsibilities: tab selection, content extraction,
tabId resolution, per-tab caching. The LLM call, schema, prompt
construction, and structured output parsing are handled by classify().
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def infer_browsing_activity(
    *,
    since: str = "1h",
    theories: list[str] | None = None,
    tab_limit: int = 5,
    max_chars_per_tab: int = 3000,
) -> dict[str, Any]:
    """Infer what the user is working on from engaged Chrome tabs.

    Two-task pipeline:
    1. **Summarize** — extract structured PageSummary per tab (cached).
    2. **Classify** — if theories are provided, classify summaries against
       intents (not cached, since intents change).

    Args:
        since: Lookback window (relative shorthand or ISO datetime).
        theories: Intent hypotheses for multi-label classification. If None,
            only summarization is performed (cheaper).
        tab_limit: Max tabs to read content from (default 5).
        max_chars_per_tab: Max characters to extract per tab (default 3000).

    Returns:
        Dict with summaries, optional classification, and metadata.
    """
    from work_buddy.journal import user_now

    now = user_now().replace(tzinfo=None)
    since_iso = _parse_relative(since, now)

    # Step 1: Get engaged tabs
    selected = _select_tabs(since_iso, limit=tab_limit)
    if not selected:
        return {
            "summaries": [],
            "classification": None,
            "tabs_read": 0,
            "tabs_cached": 0,
            "tabs_failed": 0,
        }

    # Step 2: Resolve URLs → tabIds and extract content
    urls = [t["url"] for t in selected]
    url_to_tab_id = _resolve_tab_ids(urls)
    tab_contents, tabs_failed = _extract_uncached(selected, url_to_tab_id, max_chars_per_tab)

    # Step 3: Summarize — one Haiku call for all uncached tabs
    summaries, tabs_cached = _summarize_tabs(selected, tab_contents)

    # Step 4: Optionally classify against theories
    classification = None
    if theories:
        classification = _classify_summaries(selected, summaries, theories)

    return {
        "summaries": [_summary_to_dict(s) for s in summaries],
        "classification": classification,
        "tabs_read": len(tab_contents),
        "tabs_cached": tabs_cached,
        "tabs_failed": tabs_failed,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_relative(since: str, now: datetime) -> str:
    """Convert relative shorthand to ISO string."""
    match = re.fullmatch(r"(\d+)\s*(m|min|h|hour|hours|d|day|days)", since.strip())
    if match:
        amount = int(match.group(1))
        unit = match.group(2)[0]
        deltas = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}
        return (now - deltas[unit]).isoformat()
    return since


def _extract_uncached(
    selected: list[dict],
    url_to_tab_id: dict[str, int],
    max_chars: int,
) -> tuple[dict[str, dict], int]:
    """Extract content for tabs that aren't cached. Returns (contents, failed_count)."""
    tab_contents: dict[str, dict] = {}
    tabs_failed = 0

    tab_ids_to_fetch = []
    url_for_tab_id: dict[int, str] = {}
    for tab in selected:
        url = tab["url"]
        tid = url_to_tab_id.get(url)
        if tid is not None:
            tab_ids_to_fetch.append(tid)
            url_for_tab_id[tid] = url

    if tab_ids_to_fetch:
        extracted = _extract_content(tab_ids_to_fetch, max_chars)
        for item in extracted:
            tid = item.get("tabId")
            url = url_for_tab_id.get(tid, item.get("url", ""))
            if item.get("error"):
                tabs_failed += 1
                logger.debug("Content extraction failed for %s: %s", url, item["error"])
            else:
                tab_contents[url] = item

    return tab_contents, tabs_failed


def _summarize_tabs(
    selected: list[dict],
    tab_contents: dict[str, dict],
) -> tuple[list, int]:
    """Summarize tabs using the summarize module. Returns (summaries, cached_count).

    Checks per-tab cache first. Uncached tabs are batch-summarized in one
    Haiku call. Results are cached for future calls.
    """
    from work_buddy.llm.cache import get as cache_get, put as cache_put
    from work_buddy.llm.cost import log_call
    from work_buddy.llm.summarize import PageSummary, summarize_batch

    summaries: list[PageSummary] = [None] * len(selected)  # type: ignore
    uncached_indices: list[int] = []
    tabs_cached = 0

    # Check cache per tab
    for i, tab in enumerate(selected):
        url = tab["url"]
        cache_key = f"summarize_tab:{_normalize_for_cache(url)}"
        cached = cache_get(cache_key)
        if cached:
            tabs_cached += 1
            r = cached["result"]
            from work_buddy.llm.summarize import TypedEntity
            summaries[i] = PageSummary(
                content_summary=r.get("content_summary", ""),
                entities=[TypedEntity(**e) for e in r.get("entities", [])],
                key_claims=r.get("key_claims", []),
                user_intent_speculation=r.get("user_intent_speculation", ""),
                user_posture=r.get("user_posture", "referencing"),
                source_label=_tab_label(tab),
                cached=True,
            )
            log_call(
                model=load_config().get("llm", {}).get("default_model", "claude-haiku-4-5-20251001"),
                input_tokens=0, output_tokens=0,
                task_id=cache_key, cached=True,
            )
        else:
            uncached_indices.append(i)

    # Batch-summarize uncached tabs
    if uncached_indices:
        batch_items = []
        for i in uncached_indices:
            tab = selected[i]
            url = tab["url"]
            text = tab_contents.get(url, {}).get("text", "")
            meta_desc = tab_contents.get(url, {}).get("meta", {}).get("description", "")
            if meta_desc:
                text = f"[Meta: {meta_desc[:150]}]\n{text}"
            batch_items.append({"text": text, "label": _tab_label(tab)})

        batch_results = summarize_batch(batch_items)

        for j, i in enumerate(uncached_indices):
            if j < len(batch_results):
                summaries[i] = batch_results[j]

                # Cache the result
                tab = selected[i]
                url = tab["url"]
                cache_key = f"summarize_tab:{_normalize_for_cache(url)}"
                content_text = tab_contents.get(url, {}).get("text", "")
                cache_put(
                    task_id=cache_key,
                    result={
                        "content_summary": batch_results[j].content_summary,
                        "entities": [{"name": e.name, "type": e.type, "context": e.context} for e in batch_results[j].entities],
                        "key_claims": batch_results[j].key_claims,
                        "user_intent_speculation": batch_results[j].user_intent_speculation,
                        "user_posture": batch_results[j].user_posture,
                    },
                    content_hash=hashlib.md5(content_text.encode()).hexdigest() if content_text else None,
                    content_sample=content_text[:500] if content_text else None,
                    ttl_minutes=30,
                )

    # Fill any remaining None entries
    for i in range(len(summaries)):
        if summaries[i] is None:
            summaries[i] = PageSummary(
                content_summary="Content unavailable",
                entities=[], key_claims=[],
                user_intent_speculation="", user_posture="referencing",
                source_label=_tab_label(selected[i]),
            )

    return summaries, tabs_cached


def _classify_summaries(
    selected: list[dict],
    summaries: list,
    theories: list[str],
) -> dict[str, Any]:
    """Classify summarized tabs against intent theories."""
    from work_buddy.collectors.chrome_ledger import get_tab_context
    from work_buddy.llm.classify import classify
    from work_buddy.llm.intent import DataItem

    # Build DataItems from summaries (not raw text — cheaper input)
    data_items = []
    for i, (tab, summary) in enumerate(zip(selected, summaries)):
        text = f"{summary.content_summary}\nKey claims: {'; '.join(summary.key_claims)}"
        data_items.append(DataItem(
            text=text,
            label=summary.source_label or _tab_label(tab),
            source="chrome_tab",
            metadata={
                "engaged": tab.get("engaged_count", 0),
                "visits": tab.get("visit_count", 0),
                "focused": tab.get("active_count", 0),
                "user_posture": summary.user_posture,
            },
        ))

    # Get window layout as context
    tab_context = get_tab_context()
    context_str = _format_window_context(tab_context)

    result = classify(items=data_items, intents=theories, context=context_str)

    return {
        "items": [
            {
                "label": item.label,
                "intent_matches": [
                    {"intent": m.intent, "relevant": m.relevant, "confidence": m.confidence,
                     "evidence": m.evidence, "strength": m.strength}
                    for m in item.intent_matches
                ],
            }
            for item in result.items
        ],
        "overall_narrative": result.overall_narrative,
        "activity_domains": result.activity_domains,
        "confidence": result.confidence,
        "tokens": result.tokens,
    }


def _tab_label(tab: dict) -> str:
    """Build a compact label for a tab."""
    url = tab.get("url", "")
    domain = urlparse(url).netloc
    title = tab.get("title", "")[:60]
    return f"{title} [{domain}]" if domain else title


def _format_window_context(tab_context: dict) -> str:
    """Format tab window layout as a context string for classify()."""
    windows = tab_context.get("windows", [])
    if not windows:
        return ""
    lines = ["Tab layout across windows:"]
    for w in windows:
        tab_labels = []
        for t in w.get("tabs", []):
            active = " *" if t.get("active") else ""
            tab_labels.append(f"{t.get('title', '')[:40]} [{t.get('domain', '')}]{active}")
        lines.append(f"Window ({w['tab_count']} tabs): {' | '.join(tab_labels[:8])}")
        if w["tab_count"] > 8:
            lines.append(f"  ...and {w['tab_count'] - 8} more")
    return "\n".join(lines)


def _summary_to_dict(summary: Any) -> dict:
    """Convert a PageSummary to a JSON-friendly dict."""
    return {
        "source_label": summary.source_label,
        "content_summary": summary.content_summary,
        "entities": [{"name": e.name, "type": e.type, "context": e.context} for e in summary.entities],
        "key_claims": summary.key_claims,
        "user_intent_speculation": summary.user_intent_speculation,
        "user_posture": summary.user_posture,
        "cached": summary.cached,
    }


def _normalize_for_cache(url: str) -> str:
    """Normalize URL for cache key — strip query params, fragments."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    # Keep query for pages where it matters (search, chatgpt conversations)
    if parsed.query and any(k in parsed.netloc for k in ("google.com", "chatgpt.com")):
        return f"{parsed.netloc}{path}?{parsed.query}"
    return f"{parsed.netloc}{path}"


def _select_tabs(since_iso: str, limit: int) -> list[dict]:
    """Select high-engagement tabs worth reading.

    Args:
        since_iso: Must be an ISO datetime string (not relative shorthand).
    """
    from work_buddy.collectors.chrome_ledger import get_hot_tabs

    hot = get_hot_tabs(since_iso, limit=limit * 3)
    tabs = hot.get("tabs", [])

    # Filter to actually-engaged tabs
    engaged = [
        t for t in tabs
        if t.get("engaged_count", 0) > 0 or t.get("visit_count", 0) > 0 or t.get("active_count", 0) > 0
    ]

    # Fall back to highest-scored tabs if nothing is engaged
    if not engaged:
        engaged = tabs[:limit]

    return engaged[:limit]


def _resolve_tab_ids(target_urls: list[str]) -> dict[str, int]:
    """Map URLs to Chrome tabIds from the latest snapshot."""
    from work_buddy.collectors.chrome_ledger import _read_ledger, _normalize_url

    snapshots = _read_ledger()
    if not snapshots:
        return {}

    latest = snapshots[-1]
    url_to_id: dict[str, int] = {}
    for tab in latest.get("tabs", []):
        norm = _normalize_url(tab.get("url", ""))
        if norm and tab.get("tabId"):
            url_to_id[norm] = tab["tabId"]

    # Match target URLs to tabIds
    result: dict[str, int] = {}
    for url in target_urls:
        from work_buddy.collectors.chrome_ledger import _normalize_url as norm_fn

        norm = norm_fn(url)
        if norm in url_to_id:
            result[url] = url_to_id[norm]

    return result


def _extract_content(tab_ids: list[int], max_chars: int) -> list[dict]:
    """Extract page text from Chrome tabs."""
    from work_buddy.collectors.chrome_collector import request_content

    results = request_content(tab_ids=tab_ids, max_chars=max_chars, timeout_seconds=30)
    return results or []


