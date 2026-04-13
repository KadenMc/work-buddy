"""Rolling Chrome tab snapshot ledger with query-time computed views.

Stores raw tab snapshots (captured every 5 minutes by the Chrome extension)
in a JSON ledger file with a configurable rolling window (default 7 days).
All agent-facing output is computed from raw snapshots at query time.

Data flow:
  Chrome extension alarm → native host (periodic_snapshot action)
  → append_snapshot() writes to ledger file
  → query functions compute views from raw snapshots

Storage: ~2-5 KB per snapshot, ~2 MB/week at 5-min intervals.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from work_buddy.config import load_config
from work_buddy.paths import resolve

logger = logging.getLogger(__name__)

_DEFAULT_LEDGER_PATH = resolve("chrome/ledger")
_DEFAULT_WINDOW_DAYS = 7
_DEFAULT_SNAPSHOT_INTERVAL_MINUTES = 5


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_config() -> dict[str, Any]:
    """Load chrome ledger config from config.yaml."""
    cfg = load_config()
    return cfg.get("chrome", {})


def _ledger_path() -> Path:
    chrome_cfg = _get_config()
    custom = chrome_cfg.get("ledger_path")
    if custom:
        return Path(custom)
    return _DEFAULT_LEDGER_PATH


def _window_days() -> int:
    return _get_config().get("ledger_window_days", _DEFAULT_WINDOW_DAYS)


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------


def _read_ledger() -> list[dict]:
    """Read raw snapshots from the ledger file."""
    path = _ledger_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        # Handle wrapper format
        return data.get("snapshots", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read chrome ledger: %s", e)
        return []


def _write_ledger(snapshots: list[dict]) -> None:
    """Write snapshots to the ledger file atomically."""
    path = _ledger_path()
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(
            json.dumps(snapshots, ensure_ascii=False),
            encoding="utf-8",
        )
        temp.replace(path)
    except OSError as e:
        logger.error("Failed to write chrome ledger: %s", e)


def _prune_old_snapshots(snapshots: list[dict], window_days: int) -> list[dict]:
    """Remove snapshots older than the rolling window."""
    cutoff = datetime.now() - timedelta(days=window_days)
    cutoff_iso = cutoff.isoformat()
    return [s for s in snapshots if s.get("captured_at", "") >= cutoff_iso]


# ---------------------------------------------------------------------------
# Write API (called by native host)
# ---------------------------------------------------------------------------


def append_snapshot(snapshot: dict) -> int:
    """Append a tab snapshot to the ledger and prune old entries.

    Args:
        snapshot: Raw snapshot dict from the Chrome extension containing
            at minimum ``captured_at`` (ISO timestamp) and ``tabs`` (list).

    Returns:
        Total number of snapshots in the ledger after append.
    """
    snapshots = _read_ledger()
    snapshots.append(snapshot)
    snapshots = _prune_old_snapshots(snapshots, _window_days())
    _write_ledger(snapshots)
    return len(snapshots)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _parse_ts(iso_str: str) -> datetime:
    """Parse an ISO timestamp string to a naive datetime."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        from work_buddy.config import USER_TZ
        dt = dt.astimezone(USER_TZ).replace(tzinfo=None)
    return dt


def _normalize_url(url: str) -> str:
    """Normalize a URL for comparison (strip fragments, trailing slashes)."""
    parsed = urlparse(url)
    # Skip chrome:// and extension:// URLs
    if parsed.scheme in ("chrome", "chrome-extension", "about", "devtools"):
        return ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}{('?' + parsed.query) if parsed.query else ''}"


def _domain(url: str) -> str:
    """Extract the domain from a URL."""
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def _is_noise_url(url: str) -> bool:
    """Filter out chrome:// and extension internal URLs."""
    return (
        not url
        or url.startswith("chrome://")
        or url.startswith("chrome-extension://")
        or url.startswith("about:")
        or url.startswith("devtools://")
    )


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------


def get_tabs_at(timestamp: str | datetime) -> dict[str, Any]:
    """What tabs were open at (or nearest to) a given time.

    Args:
        timestamp: ISO datetime or naive datetime.

    Returns:
        Dict with ``snapshot_time``, ``tabs`` list, ``tab_count``,
        and ``time_delta_seconds`` (how far from the requested time).
    """
    if isinstance(timestamp, str):
        target = _parse_ts(timestamp)
    else:
        target = timestamp

    snapshots = _read_ledger()
    if not snapshots:
        return {"snapshot_time": None, "tabs": [], "tab_count": 0, "time_delta_seconds": None}

    # Find nearest snapshot
    best = None
    best_delta = None
    for snap in snapshots:
        try:
            snap_time = _parse_ts(snap["captured_at"])
        except (KeyError, ValueError):
            continue
        delta = abs((snap_time - target).total_seconds())
        if best_delta is None or delta < best_delta:
            best = snap
            best_delta = delta

    if best is None:
        return {"snapshot_time": None, "tabs": [], "tab_count": 0, "time_delta_seconds": None}

    tabs = [
        {
            "url": t.get("url", ""),
            "title": t.get("title", ""),
            "active": t.get("active", False),
            "pinned": t.get("pinned", False),
        }
        for t in best.get("tabs", [])
        if not _is_noise_url(t.get("url", ""))
    ]

    return {
        "snapshot_time": best["captured_at"],
        "tabs": tabs,
        "tab_count": len(tabs),
        "time_delta_seconds": int(best_delta),
    }


def get_tab_changes(since: str | datetime, until: str | datetime | None = None) -> dict[str, Any]:
    """Tabs opened, closed, and navigated during a time window.

    Diffs consecutive snapshots within the window to find changes.

    Args:
        since: Start of window (ISO or datetime).
        until: End of window (ISO or datetime). Default: now.

    Returns:
        Dict with ``opened``, ``closed``, ``navigated`` lists and counts.
    """
    if isinstance(since, str):
        since_dt = _parse_ts(since)
    else:
        since_dt = since
    if until is None:
        until_dt = datetime.now()
    elif isinstance(until, str):
        until_dt = _parse_ts(until)
    else:
        until_dt = until

    snapshots = _read_ledger()

    # Filter and sort snapshots in the window
    window_snaps = []
    for snap in snapshots:
        try:
            ts = _parse_ts(snap["captured_at"])
        except (KeyError, ValueError):
            continue
        if since_dt <= ts <= until_dt:
            window_snaps.append((ts, snap))
    window_snaps.sort(key=lambda x: x[0])

    if len(window_snaps) < 2:
        return {
            "opened": [], "closed": [], "navigated": [],
            "opened_count": 0, "closed_count": 0, "navigated_count": 0,
            "snapshots_compared": len(window_snaps),
        }

    opened: list[dict] = []
    closed: list[dict] = []
    navigated: list[dict] = []
    engaged: list[dict] = []  # tabs where lastAccessed changed
    moved: list[dict] = []    # tabs that changed windowId

    interval = _get_config().get("snapshot_interval_minutes", _DEFAULT_SNAPSHOT_INTERVAL_MINUTES)
    interval_ms = interval * 60 * 1000

    for i in range(len(window_snaps) - 1):
        prev_ts, prev_snap = window_snaps[i]
        curr_ts, curr_snap = window_snaps[i + 1]

        # Build tabId → tab maps
        prev_by_id = {t["tabId"]: t for t in prev_snap.get("tabs", []) if t.get("tabId")}
        curr_by_id = {t["tabId"]: t for t in curr_snap.get("tabs", []) if t.get("tabId")}

        prev_ids = set(prev_by_id.keys())
        curr_ids = set(curr_by_id.keys())

        # New tabs (tabId appeared)
        for tid in curr_ids - prev_ids:
            tab = curr_by_id[tid]
            if not _is_noise_url(tab.get("url", "")):
                opened.append({
                    "url": tab.get("url", ""),
                    "title": tab.get("title", ""),
                    "time": curr_snap.get("captured_at", ""),
                })

        # Closed tabs (tabId disappeared)
        for tid in prev_ids - curr_ids:
            tab = prev_by_id[tid]
            if not _is_noise_url(tab.get("url", "")):
                closed.append({
                    "url": tab.get("url", ""),
                    "title": tab.get("title", ""),
                    "time": curr_snap.get("captured_at", ""),
                })

        # Tabs that persisted — check for navigation, engagement, movement
        for tid in prev_ids & curr_ids:
            prev_tab = prev_by_id[tid]
            curr_tab = curr_by_id[tid]
            prev_url = _normalize_url(prev_tab.get("url", ""))
            curr_url = _normalize_url(curr_tab.get("url", ""))

            if _is_noise_url(curr_tab.get("url", "")):
                continue

            # Navigated (same tabId, different URL)
            if prev_url and curr_url and prev_url != curr_url:
                navigated.append({
                    "from_url": prev_tab.get("url", ""),
                    "to_url": curr_tab.get("url", ""),
                    "to_title": curr_tab.get("title", ""),
                    "time": curr_snap.get("captured_at", ""),
                })

            # Engaged: lastAccessed changed → user interacted with this tab
            prev_la = prev_tab.get("lastAccessed")
            curr_la = curr_tab.get("lastAccessed")
            if prev_la and curr_la and curr_la != prev_la:
                delta_ms = curr_la - prev_la
                engaged.append({
                    "url": curr_tab.get("url", ""),
                    "title": curr_tab.get("title", ""),
                    "time": curr_snap.get("captured_at", ""),
                    "was_active": curr_tab.get("active", False),
                    "delta_ms": delta_ms,
                })

            # Moved: windowId changed → tab reorganization
            prev_wid = prev_tab.get("windowId")
            curr_wid = curr_tab.get("windowId")
            if prev_wid and curr_wid and prev_wid != curr_wid:
                moved.append({
                    "url": curr_tab.get("url", ""),
                    "title": curr_tab.get("title", ""),
                    "time": curr_snap.get("captured_at", ""),
                    "from_window": prev_wid,
                    "to_window": curr_wid,
                })

    # Deduplicate opened/closed by URL
    seen_opened = set()
    deduped_opened = []
    for item in opened:
        key = _normalize_url(item["url"])
        if key not in seen_opened:
            seen_opened.add(key)
            deduped_opened.append(item)

    seen_closed = set()
    deduped_closed = []
    for item in closed:
        key = _normalize_url(item["url"])
        if key not in seen_closed:
            seen_closed.add(key)
            deduped_closed.append(item)

    # Aggregate engagement per URL with delta magnitude
    engaged_by_url: dict[str, dict] = {}
    for item in engaged:
        key = _normalize_url(item["url"])
        if key not in engaged_by_url:
            engaged_by_url[key] = {
                "url": item["url"],
                "title": item["title"],
                "interaction_count": 0,
                "first_interaction": item["time"],
                "last_interaction": item["time"],
                "active_during_interaction": 0,
                "total_delta_ms": 0,
            }
        entry = engaged_by_url[key]
        entry["interaction_count"] += 1
        entry["last_interaction"] = item["time"]
        entry["total_delta_ms"] += item.get("delta_ms", 0)
        if item.get("was_active"):
            entry["active_during_interaction"] += 1

    # Compute engagement intensity: what fraction of the interval was spent
    for entry in engaged_by_url.values():
        total_possible_ms = entry["interaction_count"] * interval_ms
        if total_possible_ms > 0:
            # Clamp to 100% — delta can exceed interval if Chrome batches updates
            intensity = min(1.0, entry["total_delta_ms"] / total_possible_ms)
            entry["intensity"] = round(intensity, 2)
            total_sec = entry["total_delta_ms"] / 1000
            entry["estimated_active_seconds"] = round(total_sec)
        else:
            entry["intensity"] = 0
            entry["estimated_active_seconds"] = 0

    engaged_list = sorted(
        engaged_by_url.values(),
        key=lambda x: x["total_delta_ms"],
        reverse=True,
    )

    # Collect incremental history visits from all snapshots in the window.
    # Each snapshot may contain a "history" array of pages visited since
    # the previous snapshot (incremental, not cumulative).
    visited_by_url: dict[str, dict] = {}
    for _, snap in window_snaps:
        for item in snap.get("history", []):
            url = item.get("url", "")
            if not url or _is_noise_url(url):
                continue
            norm = _normalize_url(url)
            if not norm:
                continue
            visit_time = item.get("lastVisitTime")
            # Convert ms-epoch to ISO for display
            visit_iso = ""
            if visit_time:
                try:
                    visit_iso = datetime.fromtimestamp(visit_time / 1000).isoformat()
                except (ValueError, OSError):
                    pass
            if norm not in visited_by_url:
                visited_by_url[norm] = {
                    "url": url,
                    "title": item.get("title", ""),
                    "visit_count": 0,
                    "first_visit": visit_iso,
                    "last_visit": visit_iso,
                }
            entry = visited_by_url[norm]
            entry["visit_count"] += 1
            if visit_iso:
                if not entry["first_visit"] or visit_iso < entry["first_visit"]:
                    entry["first_visit"] = visit_iso
                if not entry["last_visit"] or visit_iso > entry["last_visit"]:
                    entry["last_visit"] = visit_iso
            # Keep the most recent title
            if item.get("title"):
                entry["title"] = item["title"]

    visited_list = sorted(
        visited_by_url.values(),
        key=lambda x: x["last_visit"],
        reverse=True,
    )

    return {
        "opened": deduped_opened,
        "closed": deduped_closed,
        "navigated": navigated,
        "engaged": engaged_list,
        "moved": moved,
        "visited": visited_list,
        "opened_count": len(deduped_opened),
        "closed_count": len(deduped_closed),
        "navigated_count": len(navigated),
        "engaged_count": len(engaged_list),
        "moved_count": len(moved),
        "visited_count": len(visited_list),
        "snapshots_compared": len(window_snaps),
    }


def get_hot_tabs(
    since: str | datetime,
    until: str | datetime | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Tabs ranked by engagement — not just presence, but actual interaction.

    Scoring uses three signals per tab:
    - **presence**: how many snapshots the tab appeared in (open duration)
    - **active**: how often it was the focused tab at snapshot time
    - **engaged**: how often ``lastAccessed`` changed between consecutive
      snapshots — proving the user interacted, even without being focused
      at the exact snapshot moment

    The composite score weights engagement heavily since many tabs sit open
    for days without interaction.

    Args:
        since: Start of window.
        until: End of window. Default: now.
        limit: Max tabs to return.

    Returns:
        Dict with ``tabs`` list ranked by engagement, plus ``window`` info.
    """
    if isinstance(since, str):
        since_dt = _parse_ts(since)
    else:
        since_dt = since
    if until is None:
        until_dt = datetime.now()
    elif isinstance(until, str):
        until_dt = _parse_ts(until)
    else:
        until_dt = until

    snapshots = _read_ledger()

    # Filter and sort snapshots in the window
    window_snaps = []
    for snap in snapshots:
        try:
            ts = _parse_ts(snap["captured_at"])
        except (KeyError, ValueError):
            continue
        if since_dt <= ts <= until_dt:
            window_snaps.append(snap)
    window_snaps.sort(key=lambda s: s.get("captured_at", ""))

    total_snapshots = len(window_snaps)

    # Per-URL accumulators
    url_presence: dict[str, int] = defaultdict(int)
    url_active: dict[str, int] = defaultdict(int)
    url_engaged: dict[str, int] = defaultdict(int)  # lastAccessed changed
    url_visits: dict[str, int] = defaultdict(int)    # history visit count
    url_titles: dict[str, str] = {}
    url_first_seen: dict[str, str] = {}
    url_last_seen: dict[str, str] = {}

    # Track lastAccessed per tabId across snapshots for engagement detection
    prev_la_by_tab: dict[int, float | None] = {}
    prev_url_by_tab: dict[int, str] = {}

    for snap_idx, snap in enumerate(window_snaps):
        curr_la_by_tab: dict[int, float | None] = {}
        curr_url_by_tab: dict[int, str] = {}

        for tab in snap.get("tabs", []):
            url = tab.get("url", "")
            if _is_noise_url(url):
                continue
            norm = _normalize_url(url)
            if not norm:
                continue
            tid = tab.get("tabId")
            la = tab.get("lastAccessed")

            url_presence[norm] += 1
            url_titles[norm] = tab.get("title", "") or url_titles.get(norm, "")
            if tab.get("active"):
                url_active[norm] += 1

            snap_time = snap.get("captured_at", "")
            if norm not in url_first_seen or snap_time < url_first_seen[norm]:
                url_first_seen[norm] = snap_time
            if norm not in url_last_seen or snap_time > url_last_seen[norm]:
                url_last_seen[norm] = snap_time

            if tid is not None:
                curr_la_by_tab[tid] = la
                curr_url_by_tab[tid] = norm

                # Engagement: lastAccessed changed since previous snapshot
                if snap_idx > 0 and tid in prev_la_by_tab:
                    prev_la = prev_la_by_tab[tid]
                    if prev_la is not None and la is not None and la != prev_la:
                        url_engaged[norm] += 1

        # Accumulate incremental history visits
        for item in snap.get("history", []):
            h_url = item.get("url", "")
            if _is_noise_url(h_url):
                continue
            h_norm = _normalize_url(h_url)
            if h_norm:
                url_visits[h_norm] += 1
                if item.get("title"):
                    url_titles[h_norm] = item["title"]

        prev_la_by_tab = curr_la_by_tab
        prev_url_by_tab = curr_url_by_tab

    # Score: engagement + visits dominate, presence is tiebreaker.
    # History visits prove navigation even between snapshot intervals.
    scored = []
    interval = _get_config().get("snapshot_interval_minutes", _DEFAULT_SNAPSHOT_INTERVAL_MINUTES)
    for norm_url in set(url_presence) | set(url_visits):
        presence = url_presence.get(norm_url, 0)
        engaged = url_engaged.get(norm_url, 0)
        active = url_active.get(norm_url, 0)
        visits = url_visits.get(norm_url, 0)
        # Composite: 5 per engagement, 4 per history visit, 3 per active, 1 per presence
        score = engaged * 5 + visits * 4 + active * 3 + presence
        scored.append({
            "url": norm_url,
            "title": url_titles.get(norm_url, ""),
            "domain": _domain(norm_url),
            "snapshot_count": presence,
            "active_count": active,
            "engaged_count": engaged,
            "visit_count": visits,
            "score": score,
            "estimated_open_minutes": presence * interval,
            "first_seen": url_first_seen.get(norm_url, ""),
            "last_seen": url_last_seen.get(norm_url, ""),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    return {
        "tabs": scored[:limit],
        "total_unique_urls": len(scored),
        "total_snapshots": total_snapshots,
        "window": {"since": since_dt.isoformat(), "until": until_dt.isoformat()},
    }


def get_tab_sessions(
    since: str | datetime,
    until: str | datetime | None = None,
) -> dict[str, Any]:
    """Group browsing activity into sessions by domain clustering.

    A "session" is a period of sustained activity on related domains.

    Args:
        since: Start of window.
        until: End of window. Default: now.

    Returns:
        Dict with domain-level browsing session summaries.
    """
    if isinstance(since, str):
        since_dt = _parse_ts(since)
    else:
        since_dt = since
    if until is None:
        until_dt = datetime.now()
    elif isinstance(until, str):
        until_dt = _parse_ts(until)
    else:
        until_dt = until

    snapshots = _read_ledger()

    # Filter and sort snapshots in the window
    window_snaps = []
    for snap in snapshots:
        try:
            ts = _parse_ts(snap["captured_at"])
        except (KeyError, ValueError):
            continue
        if since_dt <= ts <= until_dt:
            window_snaps.append(snap)
    window_snaps.sort(key=lambda s: s.get("captured_at", ""))

    # Track domain presence and engagement across snapshots
    domain_spans: dict[str, list[str]] = defaultdict(list)  # domain → [timestamps]
    domain_urls: dict[str, set] = defaultdict(set)
    domain_titles: dict[str, set] = defaultdict(set)
    domain_active: dict[str, int] = defaultdict(int)
    domain_engaged: dict[str, int] = defaultdict(int)

    prev_la_by_tab: dict[int, float | None] = {}

    for snap_idx, snap in enumerate(window_snaps):
        curr_la_by_tab: dict[int, float | None] = {}

        for tab in snap.get("tabs", []):
            url = tab.get("url", "")
            if _is_noise_url(url):
                continue
            dom = _domain(url)
            if not dom:
                continue
            tid = tab.get("tabId")
            la = tab.get("lastAccessed")

            domain_spans[dom].append(snap["captured_at"])
            domain_urls[dom].add(url)
            domain_titles[dom].add(tab.get("title", ""))
            if tab.get("active"):
                domain_active[dom] += 1

            if tid is not None:
                curr_la_by_tab[tid] = la
                if snap_idx > 0 and tid in prev_la_by_tab:
                    prev_la = prev_la_by_tab[tid]
                    if prev_la is not None and la is not None and la != prev_la:
                        domain_engaged[dom] += 1

        prev_la_by_tab = curr_la_by_tab

    interval = _get_config().get("snapshot_interval_minutes", _DEFAULT_SNAPSHOT_INTERVAL_MINUTES)

    sessions = []
    for dom, timestamps in domain_spans.items():
        timestamps.sort()
        page_count = len(domain_urls[dom])
        duration_minutes = len(timestamps) * interval
        engaged = domain_engaged.get(dom, 0)
        sessions.append({
            "domain": dom,
            "page_count": page_count,
            "snapshot_count": len(timestamps),
            "estimated_minutes": duration_minutes,
            "active_count": domain_active.get(dom, 0),
            "engaged_count": engaged,
            "first_seen": timestamps[0],
            "last_seen": timestamps[-1],
            "sample_titles": list(domain_titles[dom] - {""})[:3],
        })

    # Sort by engagement first, then presence
    sessions.sort(key=lambda s: (s["engaged_count"], s["snapshot_count"]), reverse=True)

    return {
        "sessions": sessions,
        "total_domains": len(sessions),
        "window": {"since": since_dt.isoformat(), "until": until_dt.isoformat()},
    }


# ---------------------------------------------------------------------------
# Tab proximity / context clusters
# ---------------------------------------------------------------------------


def get_tab_context(
    timestamp: str | datetime | None = None,
) -> dict[str, Any]:
    """Analyze tab proximity and grouping at a point in time.

    Adjacent tabs in the same window often indicate related work. This
    function clusters tabs by window and position, highlighting groups
    of adjacent tabs on related domains.

    Args:
        timestamp: ISO datetime or datetime. Default: latest snapshot.

    Returns:
        Dict with per-window tab lists (in position order) and detected
        clusters of adjacent related tabs.
    """
    if timestamp is None:
        snap_data = get_tabs_at(datetime.now())
    elif isinstance(timestamp, str):
        snap_data = get_tabs_at(timestamp)
    else:
        snap_data = get_tabs_at(timestamp)

    # We need the raw snapshot for index/windowId/groupId — get_tabs_at
    # strips those. Re-fetch from the ledger directly.
    snapshots = _read_ledger()
    if not snapshots:
        return {"windows": [], "clusters": []}

    target_time = snap_data.get("snapshot_time", "")
    snap = None
    for s in snapshots:
        if s.get("captured_at") == target_time:
            snap = s
            break
    if snap is None:
        # Fallback: use the latest snapshot
        snap = snapshots[-1]

    # Organize tabs by window, sorted by index
    windows: dict[int, list[dict]] = {}
    for tab in snap.get("tabs", []):
        if _is_noise_url(tab.get("url", "")):
            continue
        wid = tab.get("windowId", 0)
        windows.setdefault(wid, []).append(tab)

    for wid in windows:
        windows[wid].sort(key=lambda t: t.get("index", 0))

    # Detect clusters: runs of adjacent tabs on the same domain
    clusters: list[dict] = []
    for wid, tabs in windows.items():
        i = 0
        while i < len(tabs):
            dom = _domain(tabs[i].get("url", ""))
            if not dom:
                i += 1
                continue
            # Find run of same domain
            j = i + 1
            while j < len(tabs) and _domain(tabs[j].get("url", "")) == dom:
                j += 1
            if j - i >= 2:
                cluster_tabs = tabs[i:j]
                clusters.append({
                    "domain": dom,
                    "window_id": wid,
                    "tab_count": j - i,
                    "titles": [t.get("title", "")[:60] for t in cluster_tabs],
                    "indices": [t.get("index", 0) for t in cluster_tabs],
                })
            i = j

    # Also detect cross-domain proximity clusters: adjacent tabs that
    # may be related (e.g., github + gitingest + chatgpt)
    # We report the full window layout and let the consumer interpret.
    window_summaries = []
    for wid, tabs in windows.items():
        window_summaries.append({
            "window_id": wid,
            "tab_count": len(tabs),
            "tabs": [
                {
                    "index": t.get("index", 0),
                    "title": t.get("title", "")[:60],
                    "domain": _domain(t.get("url", "")),
                    "active": t.get("active", False),
                    "group_id": t.get("groupId"),
                }
                for t in tabs
            ],
        })

    return {
        "snapshot_time": snap.get("captured_at", ""),
        "windows": window_summaries,
        "clusters": clusters,
    }


# ---------------------------------------------------------------------------
# Ledger status
# ---------------------------------------------------------------------------


def ledger_status() -> dict[str, Any]:
    """Ledger stats: snapshot count, date range, storage size."""
    path = _ledger_path()
    snapshots = _read_ledger()

    if not snapshots:
        return {
            "snapshot_count": 0,
            "date_range": None,
            "storage_bytes": 0,
            "ledger_path": path.as_posix(),
        }

    timestamps = []
    for s in snapshots:
        ts = s.get("captured_at")
        if ts:
            timestamps.append(ts)
    timestamps.sort()

    storage_bytes = path.stat().st_size if path.exists() else 0

    return {
        "snapshot_count": len(snapshots),
        "date_range": {
            "earliest": timestamps[0] if timestamps else None,
            "latest": timestamps[-1] if timestamps else None,
        },
        "storage_bytes": storage_bytes,
        "storage_kb": round(storage_bytes / 1024, 1),
        "ledger_path": path.as_posix(),
        "window_days": _window_days(),
    }
