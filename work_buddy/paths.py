"""Centralized path resolution for work-buddy.

Single source of truth for the repo root and the ``data/`` directory tree.
All persistent files — runtime state, caches, databases, agent artifacts —
are registered here by hierarchical ID and resolved through :func:`resolve`.

Modules should never compute paths locally via ``_REPO_ROOT / "some_file"``.
Instead::

    from work_buddy.paths import resolve
    PID_FILE = resolve("runtime/sidecar-pid")

The data root defaults to ``<repo_root>/data`` but can be overridden via
``paths.data_root`` in ``config.yaml`` (absolute path or relative to repo root).

The hierarchical ID doubles as the directory structure: ``runtime/sidecar-pid``
resolves to ``data/runtime/sidecar.pid``, keeping files organized in folders
matching the ID prefix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Singleton resource registry
# ---------------------------------------------------------------------------
# Maps hierarchical ID → relative path under data_root.
# The ID prefix (before the last /) determines the subdirectory.

RESOURCES: dict[str, str] = {
    # Runtime state — ephemeral, regenerated on each sidecar start
    "runtime/sidecar-pid":       "runtime/sidecar.pid",
    "runtime/sidecar-state":     "runtime/sidecar_state.json",
    "runtime/tool-status":       "runtime/tool_status.json",
    "runtime/agent-registry":    "runtime/agent_registry.json",

    # Caches — safe to delete anytime
    "cache/llm":                 "cache/llm_cache.json",
    "cache/chrome-tabs":         "cache/chrome_tabs.json",
    "cache/chrome-request":      "cache/chrome_tabs_request",

    # Chrome integration — rolling data
    "chrome/ledger":             "chrome/tab_ledger.json",

    # Databases — persistent stores
    "db/messages":               "db/messages.db",
    "db/tasks":                  "db/task_metadata.db",
    "db/projects":               "db/projects.db",

    # Logs
    "logs/gateway-debug":        "logs/gateway_debug.log",
    "logs/search-debug":         "logs/search_debug.log",

    # Telegram
    "runtime/telegram-chat-id":  "runtime/telegram_chat_id",
}


# ---------------------------------------------------------------------------
# Entry-level pruner registry
# ---------------------------------------------------------------------------
# Maps resource IDs to ``(callable_path, default_config)`` pairs.
# The callable is imported lazily at prune time to avoid heavy imports
# at module load.  Each callable signature:
#   prune_fn(path: Path, config: dict) -> dict
#       Returns {"pruned": int, "remaining": int, "bytes_before": int, "bytes_after": int}

PRUNERS: dict[str, tuple[str, dict[str, Any]]] = {
    "chrome/ledger": (
        "work_buddy.artifacts.prune_chrome_ledger",
        {"window_days": 7},
    ),
    "cache/llm": (
        "work_buddy.artifacts.prune_llm_cache",
        {},  # uses expires_at from each entry
    ),
    "agents/sessions": (
        "work_buddy.artifacts.prune_stale_sessions",
        {"max_age_days": 14},
    ),
    "logs/global": (
        "work_buddy.artifacts.prune_old_logs",
        {"max_age_days": 7},
    ),
}


# ---------------------------------------------------------------------------
# Core path resolution
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """Return the repository root (parent of the ``work_buddy`` package)."""
    return Path(__file__).resolve().parent.parent


def _load_data_root_from_config() -> str:
    """Read ``paths.data_root`` from config without importing config.py eagerly.

    This avoids a circular import if config.py ever imports paths.py.
    Falls back to ``"data"`` when the key is absent or config is unreadable.
    """
    config_path = repo_root() / "config.yaml"
    if not config_path.exists():
        return "data"
    try:
        import yaml

        with open(config_path) as f:
            cfg: dict[str, Any] = yaml.safe_load(f) or {}
        return cfg.get("paths", {}).get("data_root", "data")
    except Exception:
        return "data"


def data_dir(category: str = "") -> Path:
    """Return ``<data_root>/[category]/``, creating it if needed.

    Parameters
    ----------
    category:
        Optional subdirectory under the data root (e.g. ``"context"``,
        ``"runtime"``).  Pass ``""`` to get the data root itself.
    """
    raw = _load_data_root_from_config()
    root = Path(raw) if Path(raw).is_absolute() else repo_root() / raw
    target = root / category if category else root
    target.mkdir(parents=True, exist_ok=True)
    return target


def resolve(resource_id: str) -> Path:
    """Resolve a hierarchical resource ID to its file path.

    The resource must be registered in :data:`RESOURCES`.  The parent
    directory is created automatically.

    Examples::

        resolve("runtime/sidecar-pid")   # → data/runtime/sidecar.pid
        resolve("chrome/ledger")          # → data/chrome/tab_ledger.json
        resolve("db/messages")            # → data/db/messages.db

    Raises ``KeyError`` for unregistered IDs.
    """
    if resource_id not in RESOURCES:
        raise KeyError(
            f"Unknown resource ID: {resource_id!r}. "
            f"Register it in work_buddy.paths.RESOURCES first."
        )
    rel = RESOURCES[resource_id]
    raw = _load_data_root_from_config()
    root = Path(raw) if Path(raw).is_absolute() else repo_root() / raw
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    return target
