"""File-backed workflow view store for the dashboard.

Workflow views are ephemeral interactive UIs (triage, review, etc.)
dispatched by agents via the DashboardSurface. Views are persisted
to a JSON file so they survive dashboard restarts (common in dev mode
with auto-reload).

Thread-safe: all mutations go through a Lock since Flask may handle
concurrent requests (from the browser poll + MCP transport).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_views: dict[str, dict[str, Any]] = {}

# Persist to a file alongside the dashboard
from work_buddy.paths import data_dir

_STORE_PATH = data_dir("agents") / "dashboard_views.json"


def _load() -> None:
    """Load views from disk into memory (called once at import time)."""
    global _views
    if _STORE_PATH.exists():
        try:
            data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            _views = {v["view_id"]: v for v in data if isinstance(v, dict)}
            logger.info("Loaded %d workflow views from disk", len(_views))
        except Exception as e:
            logger.warning("Failed to load workflow views: %s", e)
            _views = {}


def _save() -> None:
    """Persist views to disk. Must be called with _lock held."""
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(list(_views.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(_STORE_PATH)
    except Exception as e:
        logger.warning("Failed to persist workflow views: %s", e)


# Load on import
_load()


def create_view(
    view_id: str,
    title: str,
    view_type: str,
    payload: dict,
    body: str = "",
    response_type: str = "none",
    short_id: str | None = None,
    choices: list | None = None,
    expandable: bool | None = None,
) -> dict:
    """Create a new workflow view. Returns the stored view dict."""
    view = {
        "view_id": view_id,
        "title": title,
        "body": body,
        "view_type": view_type,
        "payload": payload,
        "response_type": response_type,
        "short_id": short_id,
        "choices": choices,
        "expandable": expandable,
        "status": "active",
        "created_at": time.time(),
        "responded_at": None,
        "response": None,
    }
    with _lock:
        _views[view_id] = view
        _save()
    return view


def list_views() -> list[dict]:
    """List all active (non-dismissed) views, newest first."""
    with _lock:
        return sorted(
            [v for v in _views.values() if v["status"] in ("active", "responded")],
            key=lambda v: v["created_at"],
            reverse=True,
        )


def get_view(view_id: str) -> dict | None:
    """Get a single view by ID."""
    with _lock:
        return _views.get(view_id)


def submit_response(view_id: str, response_data: Any) -> bool:
    """Record the user's response to a view. Returns False if view not found."""
    with _lock:
        view = _views.get(view_id)
        if not view:
            return False
        view["status"] = "responded"
        view["responded_at"] = time.time()
        view["response"] = response_data
        _save()
        return True


def get_response(view_id: str) -> dict | None:
    """Get the response for a view, or None if not yet responded."""
    with _lock:
        view = _views.get(view_id)
        if not view or view["response"] is None:
            return None
        return {
            "status": "responded",
            "value": view["response"],
            "responded_at": view["responded_at"],
        }


# ---------------------------------------------------------------------------
# Notification log (lightweight ring buffer for dashboard display)
# ---------------------------------------------------------------------------

_notif_log: list[dict] = []
_NOTIF_LOG_MAX = 50


def log_notification(entry: dict) -> None:
    """Append a notification event to the dashboard log."""
    entry.setdefault("ts", time.time())
    with _lock:
        _notif_log.append(entry)
        if len(_notif_log) > _NOTIF_LOG_MAX:
            _notif_log.pop(0)


def get_notification_log() -> list[dict]:
    """Return the notification log, newest first."""
    with _lock:
        return list(reversed(_notif_log))


def dismiss_view(view_id: str) -> bool:
    """Mark a view as dismissed (removes from active list)."""
    with _lock:
        view = _views.get(view_id)
        if not view:
            return False
        view["status"] = "dismissed"
        _save()
        return True
