"""Chrome tab adapter — converts currently-open Chrome tabs into TriageItems.

Only triage-worthy code in this package.  Reads the latest ledger snapshot
for currently-open tabs, enriches with engagement scores from a lookback
window, and attaches any cached Haiku summaries.

**Key design choice:** only currently-open tabs are returned.  If a tab
has been closed it's already "dealt with" — no need to triage it.

Runs in a **subprocess** (auto_run).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from work_buddy.logging_config import get_logger
from work_buddy.triage.items import TriageItem

logger = get_logger(__name__)


def chrome_tabs_to_items(
    engagement_window: str = "12h",
    include_summaries: bool = True,
) -> dict[str, Any]:
    """Collect currently-open Chrome tabs as TriageItems.

    Auto_run entry point — returns a JSON-serializable dict.

    1. Reads the **latest snapshot** from the ledger (= what's open now)
    2. Enriches each tab with engagement scores from a recent window
    3. Attaches any cached Haiku summaries that exist (no new LLM calls)

    Args:
        engagement_window: How far back to compute engagement scores
            (e.g. "12h", "24h").  Default 12h — scans ~140 snapshots
            at 5-min intervals.  Only affects scores, not which tabs
            are returned.
        include_summaries: If True, attach cached Haiku summaries to
            item text for better clustering.  No new LLM calls.

    Returns:
        Dict with 'success', 'items', and metadata.
    """
    from work_buddy.collectors.chrome_ledger import (
        _is_noise_url,
        _normalize_url as ledger_normalize,
        _read_ledger,
    )

    snapshots = _read_ledger()
    if not snapshots:
        return {"success": True, "items": [], "tab_count": 0, "enriched_count": 0}

    # Latest snapshot = currently-open tabs
    latest = snapshots[-1]
    current_tabs = [
        t for t in latest.get("tabs", [])
        if not _is_noise_url(t.get("url", ""))
    ]

    if not current_tabs:
        return {"success": True, "items": [], "tab_count": 0, "enriched_count": 0}

    # Trim snapshots to the engagement window BEFORE computing engagement.
    # Use ISO string comparison — avoids the expensive _parse_ts() which
    # does timezone conversion on every call (~24ms each × 800 snapshots).
    since_iso = _resolve_relative(engagement_window).isoformat()
    recent_snapshots = [
        snap for snap in snapshots
        if snap.get("captured_at", "") >= since_iso
    ]

    # Compute engagement only over the recent window
    engagement = _compute_engagement(recent_snapshots) if recent_snapshots else {}

    # Load cached summaries
    summaries_by_url: dict[str, dict] = {}
    if include_summaries:
        summaries_by_url = _load_cached_summaries(current_tabs)

    items: list[dict[str, Any]] = []
    enriched_count = 0
    seen_urls: set[str] = set()

    for tab in current_tabs:
        url = tab.get("url", "")
        norm_url = ledger_normalize(url)
        if not norm_url or norm_url in seen_urls:
            continue
        seen_urls.add(norm_url)

        title = tab.get("title", "")
        domain = _domain(url)
        eng = engagement.get(norm_url, {})

        # Build text — use summary if available, else title+domain
        summary_data = summaries_by_url.get(url)
        if summary_data:
            enriched_count += 1
            content_summary = summary_data.get("content_summary", "")
            intent_spec = summary_data.get("user_intent_speculation", "")
            posture = summary_data.get("user_posture", "")
            text = f"{title}. {content_summary}"
            if intent_spec:
                text += f" Intent: {intent_spec}"
        else:
            text = f"{title} [{domain}]" if domain else title
            posture = ""

        label = f"{title[:60]} [{domain}]" if domain else title[:60]

        item = TriageItem(
            id=_item_id(url),
            text=text,
            label=label,
            source="chrome_tab",
            url=url,
            metadata={
                "domain": domain,
                "title": title,
                "tab_id": tab.get("tabId"),
                "window_id": tab.get("windowId"),
                "group_id": tab.get("groupId", -1),
                "group_title": (tab.get("group") or {}).get("title", ""),
                "index": tab.get("index"),
                "pinned": tab.get("pinned", False),
                "active": tab.get("active", False),
                "engaged_count": eng.get("engaged_count", 0),
                "visit_count": eng.get("visit_count", 0),
                "active_count": eng.get("active_count", 0),
                "score": eng.get("score", 0),
                "user_posture": posture,
                "has_summary": bool(summary_data),
            },
        )
        items.append(item.to_dict())

    logger.info(
        "Collected %d currently-open tabs (%d with cached summaries)",
        len(items), enriched_count,
    )

    return {
        "success": True,
        "items": items,
        "tab_count": len(items),
        "enriched_count": enriched_count,
        "snapshot_time": latest.get("captured_at", ""),
    }


def _compute_engagement(
    snapshots: list[dict],
) -> dict[str, dict[str, int]]:
    """Compute engagement scores from pre-filtered snapshots.

    Caller is responsible for trimming snapshots to the desired window.

    Returns {normalized_url: {engaged_count, active_count, visit_count, score}}.
    """
    from collections import defaultdict
    from work_buddy.collectors.chrome_ledger import (
        _is_noise_url,
        _normalize_url as ledger_normalize,
    )

    url_presence: dict[str, int] = defaultdict(int)
    url_active: dict[str, int] = defaultdict(int)
    url_engaged: dict[str, int] = defaultdict(int)
    url_visits: dict[str, int] = defaultdict(int)
    prev_la_by_tab: dict[int, float | None] = {}

    for snap_idx, snap in enumerate(snapshots):
        curr_la_by_tab: dict[int, float | None] = {}
        for tab in snap.get("tabs", []):
            url = tab.get("url", "")
            if _is_noise_url(url):
                continue
            norm = ledger_normalize(url)
            if not norm:
                continue

            tid = tab.get("tabId")
            la = tab.get("lastAccessed")

            url_presence[norm] += 1
            if tab.get("active"):
                url_active[norm] += 1

            if tid is not None:
                curr_la_by_tab[tid] = la
                if snap_idx > 0 and tid in prev_la_by_tab:
                    prev_la = prev_la_by_tab[tid]
                    if prev_la is not None and la is not None and la != prev_la:
                        url_engaged[norm] += 1

        for item in snap.get("history", []):
            h_url = item.get("url", "")
            if not _is_noise_url(h_url):
                h_norm = ledger_normalize(h_url)
                if h_norm:
                    url_visits[h_norm] += 1

        prev_la_by_tab = curr_la_by_tab

    result: dict[str, dict[str, int]] = {}
    for norm in set(url_presence) | set(url_visits):
        engaged = url_engaged.get(norm, 0)
        visits = url_visits.get(norm, 0)
        active = url_active.get(norm, 0)
        presence = url_presence.get(norm, 0)
        result[norm] = {
            "engaged_count": engaged,
            "visit_count": visits,
            "active_count": active,
            "score": engaged * 5 + visits * 4 + active * 3 + presence,
        }
    return result


# ── Extract + Cluster (Tier 1 — free) ────────────────────────────


def extract_and_cluster_tabs(
    collect_result: dict[str, Any] | None = None,
    items_data: list[dict[str, Any]] | None = None,
    max_extract: int = 30,
    max_chars: int = 3000,
) -> dict[str, Any]:
    """Extract page content, embed with document tower, cluster.  Auto_run.

    This is the "free" path — no LLM calls.  Uses the Chrome extension
    to extract page text, then embeds with the asymmetric document tower
    (leaf-ir, 768d) and clusters via Louvain.

    Args:
        collect_result: Full output of chrome_tabs_to_items (from workflow
            input_map).  The 'items' key is extracted automatically.
        items_data: Direct list of TriageItem dicts (for standalone use).
            One of collect_result or items_data must be provided.
        max_extract: Max tabs to extract content from.
        max_chars: Max characters per tab for extraction.

    Returns:
        Dict with clusters, singletons, and a content_map for downstream
        use (avoids re-extracting for Haiku summarization).
    """
    from work_buddy.collectors.chrome_collector import request_content
    from work_buddy.collectors.chrome_infer import _resolve_tab_ids
    from work_buddy.triage.cluster import cluster_items, embed_items

    # Accept either the full collect result or a direct items list
    if items_data is None:
        if collect_result is not None:
            items_data = collect_result.get("items", [])
        else:
            return {"success": False, "error": "No items provided"}

    items = [TriageItem.from_dict(d) for d in items_data]

    # Resolve tab IDs for content extraction
    urls = [item.url for item in items if item.url]
    url_to_tab_id = _resolve_tab_ids(urls)

    # Extract page content
    tab_ids = []
    url_for_tid: dict[int, str] = {}
    for item in items[:max_extract]:
        if item.url:
            tid = url_to_tab_id.get(item.url)
            if tid is not None:
                tab_ids.append(tid)
                url_for_tid[tid] = item.url

    content_map: dict[str, str] = {}  # {url: page_text}
    has_content = False
    if tab_ids:
        extracted = request_content(tab_ids=tab_ids, max_chars=max_chars)
        if extracted:
            for ex in extracted:
                tid = ex.get("tabId")
                url = url_for_tid.get(tid, ex.get("url", ""))
                if not ex.get("error") and ex.get("text"):
                    content_map[url] = ex["text"]

    has_content = len(content_map) > len(items) * 0.3  # >30% success

    # Update item text with extracted content where available
    if has_content:
        for item in items:
            if item.url and item.url in content_map:
                # Prepend title for context, then page content
                title = item.metadata.get("title", item.label)
                item.text = f"{title}\n\n{content_map[item.url][:max_chars]}"

    logger.info(
        "Content extracted for %d/%d tabs (using %s model)",
        len(content_map), len(items),
        "leaf-ir document tower" if has_content else "leaf-mt on titles",
    )

    # Embed — use document tower if we have content, symmetric if titles only
    embed_items(items, use_ir_model=has_content)

    # Cluster
    clusters = cluster_items(items)

    multi = [c for c in clusters if c.size > 1]
    singletons = [c for c in clusters if c.size == 1]

    logger.info(
        "Clustering: %d multi-item clusters, %d singletons",
        len(multi), len(singletons),
    )

    # Strip embeddings from serialized output — they're only needed
    # during clustering, not downstream.  Saves ~475KB for 28 tabs.
    cluster_dicts = []
    for c in multi:
        cd = c.to_dict()
        for item in cd["items"]:
            item.pop("embedding", None)
        cluster_dicts.append(cd)

    singleton_dicts = []
    for c in singletons:
        cd = c.to_dict()
        for item in cd["items"]:
            item.pop("embedding", None)
        singleton_dicts.append(cd)

    # Write content_map to a temp file instead of inlining it.
    # The summarize step reads from this path to avoid re-extracting.
    import json as _json
    import tempfile
    content_map_path = None
    if content_map:
        fd, content_map_path = tempfile.mkstemp(
            suffix=".json", prefix="wb_content_map_"
        )
        with open(fd, "w", encoding="utf-8") as f:
            _json.dump(content_map, f)
        logger.info("Content map written to %s (%d URLs)", content_map_path, len(content_map))

    # Write content index for triage_item_detail lookups
    if content_map:
        from work_buddy.triage.detail import write_content_index
        url_to_id = {item.url: item.id for item in items if item.url}
        write_content_index(content_map, url_to_id)

    return {
        "success": True,
        "clusters": cluster_dicts,
        "singletons": singleton_dicts,
        "item_count": len(items),
        "cluster_count": len(multi),
        "singleton_count": len(singletons),
        "content_map_path": content_map_path,  # file path, not inline data
        "content_extracted": len(content_map),
        "embedding_model": "leaf-ir" if has_content else "leaf-mt",
    }


# ── Enrichment (Tier 2 — Haiku) ─────────────────────────────────


def enrich_items_with_summaries(
    clusters_data: dict[str, Any],
    max_tabs: int = 20,
) -> dict[str, Any]:
    """Summarize tabs with Haiku.  Auto_run entry point.

    Uses pre-extracted content from the extract-and-cluster step when
    available (avoids re-extracting).  Only tabs without cached summaries
    are sent to Haiku.

    Args:
        clusters_data: Output of extract_and_cluster_tabs (has clusters,
            singletons, and content_map).
        max_tabs: Maximum tabs to summarize (default 20).

    Returns:
        Updated clusters/singletons with enriched item text + summaries dict.
    """
    # Reconstruct items from clusters + singletons
    all_cluster_dicts = (
        clusters_data.get("clusters", [])
        + clusters_data.get("singletons", [])
    )
    all_items: list[TriageItem] = []
    for cd in all_cluster_dicts:
        for item_dict in cd.get("items", []):
            all_items.append(TriageItem.from_dict(item_dict))

    # Load content_map from file if a path was provided (avoids piping
    # 67KB+ of page text through subprocess stdin)
    content_map: dict[str, str] = {}
    content_map_path = clusters_data.get("content_map_path")
    if content_map_path:
        try:
            import json as _json
            with open(content_map_path, "r", encoding="utf-8") as f:
                content_map = _json.load(f)
            logger.info("Loaded content map from %s (%d URLs)", content_map_path, len(content_map))
        except Exception as e:
            logger.warning("Could not load content map from %s: %s", content_map_path, e)

    # Find tabs needing summaries
    needs_summary = [
        item for item in all_items
        if not item.metadata.get("has_summary") and item.url
    ]

    if not needs_summary:
        logger.info("All tabs already have cached summaries")
        return {
            "success": True,
            **_passthrough_clusters(clusters_data),
            "summaries": {},
            "summarized_count": 0,
            "already_cached": len(all_items),
        }

    # Prioritize by engagement
    needs_summary.sort(key=lambda i: i.metadata.get("score", 0), reverse=True)
    to_summarize = needs_summary[:max_tabs]

    logger.info(
        "Enriching %d/%d uncached tabs with Haiku summaries",
        len(to_summarize), len(needs_summary),
    )

    from work_buddy.collectors.chrome_infer import (
        _extract_content,
        _resolve_tab_ids,
        _summarize_tabs,
        _summary_to_dict,
    )

    # Build tab dicts for _summarize_tabs
    tab_dicts = []
    for item in to_summarize:
        if not item.url:
            continue
        tab_dicts.append({
            "url": item.url,
            "title": item.metadata.get("title", item.label),
            "engaged_count": item.metadata.get("engaged_count", 0),
            "visit_count": item.metadata.get("visit_count", 0),
            "active_count": item.metadata.get("active_count", 0),
        })

    # Build tab_contents — prefer content_map (already extracted), else re-extract
    tab_contents: dict[str, dict] = {}
    urls_needing_extraction = []
    for td in tab_dicts:
        url = td["url"]
        if url in content_map:
            tab_contents[url] = {"text": content_map[url], "meta": {}}
        else:
            urls_needing_extraction.append(url)

    # Extract content for any tabs not in content_map
    if urls_needing_extraction:
        url_to_tab_id = _resolve_tab_ids(urls_needing_extraction)
        tab_ids_to_fetch = []
        url_for_tid: dict[int, str] = {}
        for url in urls_needing_extraction:
            tid = url_to_tab_id.get(url)
            if tid is not None:
                tab_ids_to_fetch.append(tid)
                url_for_tid[tid] = url

        if tab_ids_to_fetch:
            extracted = _extract_content(tab_ids_to_fetch, 3000)
            for ex in extracted:
                tid = ex.get("tabId")
                url = url_for_tid.get(tid, ex.get("url", ""))
                if not ex.get("error"):
                    tab_contents[url] = ex

    # Summarize via Haiku
    summaries_list, cached_count = _summarize_tabs(tab_dicts, tab_contents)

    # Build summaries dict keyed by URL
    summaries: dict[str, dict] = {}
    for td, summary in zip(tab_dicts, summaries_list):
        summaries[td["url"]] = _summary_to_dict(summary)

    # Update items in clusters with summary data
    summarized_count = 0
    for cd in all_cluster_dicts:
        for item_dict in cd.get("items", []):
            url = item_dict.get("url")
            if url and url in summaries:
                sd = summaries[url]
                item_dict["metadata"]["has_summary"] = True
                item_dict["metadata"]["user_posture"] = sd.get("user_posture", "")
                item_dict["metadata"]["summary_data"] = sd
                summarized_count += 1

    # Write summary index for triage_item_detail lookups
    all_item_dicts = []
    for cd in all_cluster_dicts:
        all_item_dicts.extend(cd.get("items", []))
    from work_buddy.triage.detail import write_summary_index
    write_summary_index(all_item_dicts, summaries)

    return {
        "success": True,
        **_passthrough_clusters(clusters_data),
        "summaries": summaries,
        "summarized_count": summarized_count,
        "already_cached": len(all_items) - len(needs_summary),
        "content_extracted": len(tab_contents),
    }


def _passthrough_clusters(data: dict[str, Any]) -> dict[str, Any]:
    """Pass through cluster structure from previous step."""
    return {
        "clusters": data.get("clusters", []),
        "singletons": data.get("singletons", []),
        "item_count": data.get("item_count", 0),
        "cluster_count": data.get("cluster_count", 0),
        "singleton_count": data.get("singleton_count", 0),
    }


# ── Helpers ──────────────────────────────────────────────────────


def _resolve_relative(since: str) -> datetime:
    """Convert relative shorthand to datetime."""
    match = re.fullmatch(r"(\d+)\s*(m|min|h|hour|hours|d|day|days)", since.strip())
    if match:
        amount = int(match.group(1))
        unit = match.group(2)[0]
        deltas = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
        }
        return datetime.now() - deltas[unit]
    # Try ISO
    try:
        return datetime.fromisoformat(since)
    except ValueError:
        return datetime.now() - timedelta(hours=24)


def _load_cached_summaries(tabs: list[dict]) -> dict[str, dict]:
    """Load cached Haiku summaries for tabs (no new LLM calls)."""
    try:
        from work_buddy.llm.cache import get as cache_get
    except ImportError:
        return {}

    result: dict[str, dict] = {}
    for tab in tabs:
        url = tab.get("url", "")
        cache_key = f"summarize_tab:{_normalize_for_cache(url)}"
        cached = cache_get(cache_key)
        if cached and cached.get("result"):
            result[url] = cached["result"]
    return result


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def _normalize_url(url: str) -> str:
    """Lightweight URL normalization for deduplication."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return f"{parsed.netloc}{path}"


def _normalize_for_cache(url: str) -> str:
    """Match chrome_infer's cache key convention."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if parsed.query and any(
        k in parsed.netloc for k in ("google.com", "chatgpt.com")
    ):
        return f"{parsed.netloc}{path}?{parsed.query}"
    return f"{parsed.netloc}{path}"


def _item_id(url: str) -> str:
    norm = _normalize_for_cache(url)
    return f"tab_{hashlib.md5(norm.encode()).hexdigest()[:10]}"
