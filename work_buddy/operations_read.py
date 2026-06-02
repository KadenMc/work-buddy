"""Read-only access to operation records for shell-level tooling.

Operation records are durable JSON files written by the MCP gateway
(:mod:`work_buddy.mcp_server.tools.gateway`) for every ``wb_run`` dispatch.
This module exposes a read path that deliberately does **not** import the
gateway — importing it pulls in the whole MCP/FastMCP stack, which would
dominate the startup cost of a short-lived poller. The CLI in
:mod:`work_buddy.statusctl` reads operation status through here so it can
start in milliseconds.

The on-disk layout is owned by the gateway. This module mirrors only its
path resolution (``data_dir("agents")/"operations"/"<op_id>.json"``) and
field names; keep it in sync with
``gateway._get_operations_dir`` / ``gateway._load_operation`` /
``gateway._complete_operation`` if those change.

Everything here is strictly read-only: it observes operation records, it
never creates, mutates, or completes them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def operations_dir() -> Path:
    """Return the global operations directory (mirrors the gateway).

    Uses :func:`work_buddy.paths.data_dir`, the sanctioned path resolver,
    so a custom ``paths.data_root`` is honoured. ``data_dir`` creates the
    ``agents`` directory if absent — an idempotent no-op in any real
    deployment where the gateway has already run.
    """
    from work_buddy.paths import data_dir

    return data_dir("agents") / "operations"


def load_operation(op_id: str) -> dict[str, Any] | None:
    """Load an operation record by ID, or ``None`` if it does not exist.

    Mirrors ``gateway._load_operation`` without importing the gateway.
    Returns ``None`` (rather than raising) on a missing or unreadable
    file so callers can treat "not found" and "unreadable" uniformly.
    """
    path = operations_dir() / f"{op_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# Terminal record states → CLI state vocabulary. ``running`` is the only
# non-terminal state the gateway writes.
_RECORD_STATE_MAP = {
    "completed": "completed",
    "failed": "failed",
    "running": "running",
}


def operation_status(op_id: str) -> dict[str, Any]:
    """Return a normalized, read-only status view of an operation.

    Output shape (always includes ``state``)::

        {
          "operation_id": str,
          "state": "running" | "completed" | "failed" | "stale" | "not_found",
          "terminal": bool,          # True for completed/failed
          "name": str | None,        # the capability/workflow name
          "error": str | None,
          "error_kind": str | None,
          "created_at": str | None,
          "completed_at": str | None,
        }

    ``stale`` marks a record still flagged ``running`` whose execution
    lease (``locked_until``) has elapsed — the dispatching process likely
    died. It is reported informationally; it is *not* treated as terminal,
    so a ``wait`` keeps polling until the real timeout (a lease can be
    legitimately long). Callers that want to abort on staleness can check
    the field.
    """
    record = load_operation(op_id)
    if record is None:
        return {
            "operation_id": op_id,
            "state": "not_found",
            "terminal": False,
            "name": None,
            "error": None,
            "error_kind": None,
            "created_at": None,
            "completed_at": None,
        }

    raw = record.get("status")
    state = _RECORD_STATE_MAP.get(raw, raw or "running")

    if state == "running" and _lease_expired(record.get("locked_until")):
        state = "stale"

    return {
        "operation_id": op_id,
        "state": state,
        "terminal": state in ("completed", "failed"),
        "name": record.get("name"),
        "error": record.get("error"),
        "error_kind": record.get("error_kind"),
        "created_at": record.get("created_at"),
        "completed_at": record.get("completed_at"),
    }


def _lease_expired(locked_until: str | None) -> bool:
    """True when an ISO ``locked_until`` lease is in the past."""
    if not locked_until:
        return False
    try:
        deadline = datetime.fromisoformat(locked_until)
    except (ValueError, TypeError):
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= deadline
