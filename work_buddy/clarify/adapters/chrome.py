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
from work_buddy.clarify.items import TriageItem

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
    """Load cached Haiku summaries for tabs by URL (no new LLM calls).

    Best-effort lookup: we don't have the original page content here,
    so we can't compute the cache module's ``input_hash`` for a strict
    match. Read the underlying store directly and trust the URL-keyed
    cache slot. Stale entries age out via the cache's expiry field,
    which we still honour.
    """
    try:
        from datetime import datetime as _datetime

        from work_buddy.llm.cache import _read_cache
    except ImportError:
        return {}

    cache = _read_cache()
    if not cache:
        return {}

    result: dict[str, dict] = {}
    now = _datetime.now()
    for tab in tabs:
        url = tab.get("url", "")
        if not url:
            continue
        cache_key = f"summarize_tab:{_normalize_for_cache(url)}"
        entry = cache.get(cache_key)
        if not entry or "result" not in entry:
            continue
        expires_at = entry.get("expires_at") or ""
        if expires_at:
            try:
                if _datetime.fromisoformat(expires_at) < now:
                    continue
            except ValueError:
                pass
        result[url] = entry["result"]
    return result


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


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
