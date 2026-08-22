"""Collect Chrome tab data, browsing history, session info, and on-demand page content.

On-demand approach: creates a request file (with optional parameters),
waits for the extension to notice it and write .chrome_tabs.json,
then reads and formats the result.

Two request modes:
- snapshot (default): tabs + history + recently closed
- get_content: extract text from specific tabs by ID
"""

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from work_buddy.paths import resolve

_REQUEST_FILE = resolve("cache/chrome-request")
_TABS_FILE = resolve("cache/chrome-tabs")


def _remove_request_if_owned(request_id: str) -> None:
    """Remove only the still-current request written by this caller."""

    if not _REQUEST_FILE.exists():
        return
    try:
        pending = json.loads(_REQUEST_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if pending.get("request_id") == request_id:
        _REQUEST_FILE.unlink(missing_ok=True)


def request_tabs(
    timeout_seconds: int = 15,
    since: str | None = None,
    until: str | None = None,
) -> dict | None:
    """Request a fresh snapshot from the Chrome extension.

    Args:
        timeout_seconds: How long to wait for the extension to respond.
        since: ISO datetime for history range start.
        until: ISO datetime for history range end.

    Returns parsed JSON snapshot, or None if Chrome doesn't respond.
    """
    old_mtime = _TABS_FILE.stat().st_mtime if _TABS_FILE.exists() else 0

    # Write request file as JSON with optional time range.
    request_id = uuid.uuid4().hex
    request_data = {
        "request_id": request_id,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    if since:
        request_data["since"] = since
    if until:
        request_data["until"] = until

    _REQUEST_FILE.write_text(
        json.dumps(request_data),
        encoding="utf-8",
    )

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _TABS_FILE.exists():
            new_mtime = _TABS_FILE.stat().st_mtime
            if new_mtime > old_mtime:
                try:
                    with open(_TABS_FILE, encoding="utf-8") as f:
                        data = json.load(f)
                    response_id = data.get("request_id")
                    # Pre-1.3 extension workers did not echo request IDs. Reads
                    # remain backwards compatible; when an ID is present it
                    # must match so a newer correlated response cannot cross
                    # requests.
                    if response_id is not None and response_id != request_id:
                        old_mtime = new_mtime
                        continue
                    _TABS_FILE.unlink(missing_ok=True)
                    return data
                except (json.JSONDecodeError, OSError):
                    old_mtime = new_mtime
                    continue
        time.sleep(0.5)

    _remove_request_if_owned(request_id)
    return None


def request_content(
    tab_ids: list[int],
    max_chars: int = 10000,
    timeout_seconds: int = 30,
) -> list[dict] | None:
    """Request text content from specific tabs via content script injection.

    Args:
        tab_ids: List of tab IDs to extract content from.
        max_chars: Max characters to extract per tab.
        timeout_seconds: How long to wait (discarded tabs may need reloading).

    Returns list of {tabId, url, title, text, meta, error} dicts,
    or None if Chrome doesn't respond.
    """
    old_mtime = _TABS_FILE.stat().st_mtime if _TABS_FILE.exists() else 0

    request_id = uuid.uuid4().hex
    request_data = {
        "request_id": request_id,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "request_action": "get_content",
        "tab_ids": tab_ids,
        "max_chars": max_chars,
    }

    _REQUEST_FILE.write_text(
        json.dumps(request_data),
        encoding="utf-8",
    )

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _TABS_FILE.exists():
            new_mtime = _TABS_FILE.stat().st_mtime
            if new_mtime > old_mtime:
                try:
                    with open(_TABS_FILE, encoding="utf-8") as f:
                        data = json.load(f)
                    response_id = data.get("request_id")
                    if response_id is not None and response_id != request_id:
                        old_mtime = new_mtime
                        continue
                    _TABS_FILE.unlink(missing_ok=True)
                    return data.get("tab_contents", [])
                except (json.JSONDecodeError, OSError):
                    old_mtime = new_mtime
                    continue
        time.sleep(0.5)

    _remove_request_if_owned(request_id)
    return None


def _format_timestamp(ms: float | int | None) -> str:
    """Format a milliseconds-since-epoch timestamp to readable string."""
    if not ms:
        return ""
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return ""


def _format_age(ms: float | int | None) -> str:
    """Format a timestamp as relative age (e.g., '2h ago')."""
    if not ms:
        return ""
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m ago"
        if hours < 24:
            return f"{int(hours)}h ago"
        return f"{int(hours / 24)}d ago"
    except (ValueError, OSError):
        return ""


def collect(cfg: dict[str, Any]) -> str:
    """Collect Chrome context and return markdown string.

    Requests a fresh snapshot from the extension on-demand.
    Supports cfg["since"] / cfg["until"] for time-range scoping.
    """
    now = datetime.now(timezone.utc)
    range_since = cfg.get("since")
    range_until = cfg.get("until")

    tabs_data = request_tabs(
        timeout_seconds=15,
        since=range_since,
        until=range_until,
    )

    if tabs_data is None:
        return (
            "# Chrome\n\n"
            f"*Collected: {now.strftime('%Y-%m-%d %H:%M UTC')}*\n\n"
            "*Chrome data unavailable. Is Chrome running with the "
            "Work Buddy Tab Exporter extension?*\n"
        )

    tabs = tabs_data.get("tabs", [])
    tab_count = tabs_data.get("tab_count", len(tabs))
    window_ids = tabs_data.get("window_ids", [])
    history = tabs_data.get("history", [])
    recently_closed = tabs_data.get("recently_closed", [])

    lines = [
        "# Chrome",
        "",
        f"*Collected: {now.strftime('%Y-%m-%d %H:%M UTC')}*",
        f"*{tab_count} tabs across {len(window_ids)} windows*",
        "",
    ]

    # ── Open tabs ────────────────────────────────────────────────
    windows: dict[int, list[dict]] = {}
    for tab in tabs:
        wid = tab.get("windowId", 0)
        windows.setdefault(wid, []).append(tab)

    for i, (wid, win_tabs) in enumerate(windows.items(), 1):
        lines.append(f"## Window {i} ({len(win_tabs)} tabs)")
        lines.append("")
        for tab in win_tabs:
            title = tab.get("title", "Untitled")
            url = tab.get("url", "")

            flags = []
            if tab.get("pinned"):
                flags.append("pinned")
            if tab.get("active"):
                flags.append("**active**")
            if tab.get("audible"):
                flags.append("playing audio")
            if tab.get("group") and tab["group"].get("title"):
                flags.append(tab["group"]["title"])

            last_accessed = _format_age(tab.get("lastAccessed"))
            created_at = _format_age(tab.get("createdAt"))

            flag_str = f" [{', '.join(flags)}]" if flags else ""
            time_str = ""
            if last_accessed:
                time_str = f" (accessed {last_accessed})"
            elif created_at:
                time_str = f" (opened {created_at})"

            lines.append(f"- {title}{flag_str}{time_str}")
            lines.append(f"  `{url}`")
        lines.append("")

    # ── Browsing history ─────────────────────────────────────────
    if history:
        hist_range = tabs_data.get("history_range", {})
        since_str = hist_range.get("since", "24h ago")
        until_str = hist_range.get("until", "now")
        lines.append(f"## Browsing History ({len(history)} items, {since_str} → {until_str})")
        lines.append("")

        # Sort by lastVisitTime descending
        history.sort(key=lambda h: h.get("lastVisitTime", 0), reverse=True)

        for item in history[:100]:  # cap at 100 for readability
            title = item.get("title", "")
            url = item.get("url", "")
            visit_time = _format_timestamp(item.get("lastVisitTime"))
            visit_count = item.get("visitCount", 0)

            # Skip chrome:// and extension URLs
            if url.startswith("chrome://") or url.startswith("chrome-extension://"):
                continue

            count_str = f" ({visit_count}x)" if visit_count > 1 else ""
            display_title = title or url
            lines.append(f"- {visit_time} — {display_title}{count_str}")
            lines.append(f"  `{url}`")
        lines.append("")
    else:
        lines.append("## Browsing History")
        lines.append("")
        lines.append("*No history data in this window.*")
        lines.append("")

    # ── Recently closed tabs ─────────────────────────────────────
    if recently_closed:
        lines.append(f"## Recently Closed ({len(recently_closed)} tabs)")
        lines.append("")
        for tab in recently_closed:
            title = tab.get("title", "Untitled")
            url = tab.get("url", "")
            closed_at = _format_timestamp(tab.get("closedAt"))
            lines.append(f"- {closed_at} — {title}")
            lines.append(f"  `{url}`")
        lines.append("")

    return "\n".join(lines)


# ── Tab mutations ──────────────────────────────────────────────────


def _request_mutation(
    mutation: str,
    timeout_seconds: int = 15,
    **params: Any,
) -> dict | None:
    """Send a tab mutation request and wait for result.

    Args:
        mutation: Mutation type (close_tabs, group_tabs, ungroup_tabs, move_tabs).
        timeout_seconds: How long to wait for the extension.
        **params: Additional parameters for the mutation.

    Returns:
        Mutation result dict, or None if Chrome doesn't respond.
    """
    old_mtime = _TABS_FILE.stat().st_mtime if _TABS_FILE.exists() else 0
    request_id = uuid.uuid4().hex

    request_data = {
        "request_id": request_id,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "request_action": "mutate",
        "mutation": mutation,
        **params,
    }

    _REQUEST_FILE.write_text(
        json.dumps(request_data),
        encoding="utf-8",
    )

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _TABS_FILE.exists():
            new_mtime = _TABS_FILE.stat().st_mtime
            if new_mtime > old_mtime:
                old_mtime = new_mtime
                try:
                    with open(_TABS_FILE, encoding="utf-8") as f:
                        data = json.load(f)
                    # The snapshot file is shared with periodic/on-demand tab
                    # exports.  Only a response carrying our nonce proves that
                    # this launch mutation was actually handled; accepting an
                    # unrelated write here can strand a one-use identity grant
                    # while the caller falsely reports a successful reconnect.
                    response_id = data.get("request_id")
                    identity_handoff = mutation in {
                        "focus_existing_tab",
                        "focus_or_create_tab",
                    }
                    if response_id != request_id and (
                        identity_handoff or response_id is not None
                    ):
                        continue
                    _TABS_FILE.unlink(missing_ok=True)
                    result = data.get("mutation_result")
                    return result if isinstance(result, dict) else None
                except (json.JSONDecodeError, OSError):
                    continue
        time.sleep(0.5)

    _remove_request_if_owned(request_id)
    return None


def close_tabs(tab_ids: list[int], timeout_seconds: int = 15) -> dict | None:
    """Close specified Chrome tabs.

    Args:
        tab_ids: List of Chrome tab IDs to close.

    Returns:
        Result dict with closed/missing counts, or None if no response.
    """
    return _request_mutation("close_tabs", timeout_seconds, tab_ids=tab_ids)


def group_tabs(
    tab_ids: list[int],
    title: str = "",
    color: str = "grey",
    group_id: int | None = None,
    timeout_seconds: int = 15,
) -> dict | None:
    """Create a Chrome tab group or add tabs to an existing group.

    Args:
        tab_ids: Tabs to group.
        title: Group title (displayed in Chrome).
        color: Group color (grey, blue, red, yellow, green, pink, purple, cyan, orange).
        group_id: If set, add tabs to this existing group. Otherwise create new group.

    Returns:
        Result dict with group_id, or None if no response.
    """
    params: dict[str, Any] = {"tab_ids": tab_ids, "title": title, "color": color}
    if group_id is not None:
        params["group_id"] = group_id
    return _request_mutation("group_tabs", timeout_seconds, **params)


def move_tabs(
    tab_ids: list[int],
    index: int = -1,
    window_id: int | None = None,
    timeout_seconds: int = 15,
) -> dict | None:
    """Move tabs to a specific position or window.

    Args:
        tab_ids: Tabs to move.
        index: Position index (-1 = end of window).
        window_id: Target window (None = current window).

    Returns:
        Result dict, or None if no response.
    """
    params: dict[str, Any] = {"tab_ids": tab_ids, "index": index}
    if window_id is not None:
        params["window_id"] = window_id
    return _request_mutation("move_tabs", timeout_seconds, **params)


def focus_or_create_tab(
    url: str,
    target_hash: str = "",
    preserve_path: bool = False,
    timeout_seconds: int = 15,
) -> dict | None:
    """Focus an existing Chrome tab matching *url*, or create a new one.

    If an existing tab is found, it's activated and its window is focused.
    If *target_hash* is set, the tab is navigated to ``url/target_hash``.
    When *preserve_path* is true, an existing tab keeps its current path and
    query and receives only the new fragment.  This is used for trusted app
    bootstrap delivery so an open document is not replaced by the app root.
    If no matching tab exists, a new tab is created.

    Args:
        url: Base URL to match (e.g. "http://127.0.0.1:5127").
        target_hash: Optional hash fragment (e.g. "#view/req_abc123").
        preserve_path: Keep an existing matching tab's path and query.
        timeout_seconds: How long to wait for the extension.

    Returns:
        Result dict with created/focused status, or None if no response.
    """
    match_url = url.rstrip("/") or url
    return _request_mutation(
        "focus_or_create_tab",
        timeout_seconds,
        url=match_url,
        target_hash=target_hash,
        preserve_path=preserve_path,
    )


def focus_existing_tab(
    url: str,
    target_hash: str = "",
    timeout_seconds: int = 15,
) -> dict | None:
    """Deliver a fragment to an existing dashboard tab without opening one.

    This is the quiet trusted-host recovery path used by deliberate sidecar
    start/restart operations.  The extension preserves the tab's path/query,
    does not activate it, and reports ``found=false`` when no matching app tab
    exists.  A new mutation name makes older extensions fail closed instead of
    accidentally creating a browser tab while interpreting newer options.
    """

    match_url = url.rstrip("/") or url
    return _request_mutation(
        "focus_existing_tab",
        timeout_seconds,
        url=match_url,
        target_hash=target_hash,
    )
