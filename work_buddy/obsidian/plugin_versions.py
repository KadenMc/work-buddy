"""Track versions of all Obsidian plugins and work-buddy integration status.

Snapshots all installed plugin versions to a JSON file in the agent session
directory. Tracks which plugins are integrated with work-buddy and records
the last version confirmed working for each integrated plugin.

The ``last_working_confirmed`` version is stored persistently in
``agents/plugin_confirmed.json`` (survives across sessions). It gets updated
when a plugin's ``check_ready()`` succeeds, providing a reference point for
diagnosing breakage after plugin updates.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.obsidian.plugins import installed_plugins, active_plugins
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# Plugin IDs that work-buddy directly integrates with, mapped to
# the integration package that depends on them.
INTEGRATED_PLUGINS = {
    "google-calendar": "work_buddy.calendar",
    "obsidian-tasks-plugin": "work_buddy.obsidian.tasks",
    "tag-wrangler": "work_buddy.obsidian.tags",
    "smart-connections": "work_buddy.obsidian.smart",
    "obsidian-local-rest-api": "work_buddy.obsidian.bridge",
    "obsidian-day-planner": "work_buddy.obsidian.day_planner",
    "datacore": "work_buddy.obsidian.datacore",
}

_SNAPSHOT_FILENAME = "plugin_versions.json"
_CONFIRMED_FILENAME = "plugin_confirmed.json"
_cached_snapshot: dict | None = None


def _vault_root() -> Path:
    cfg = load_config()
    return Path(cfg.get("vault_root", ""))


def _agents_dir() -> Path:
    """Persistent agents directory (not per-session)."""
    from work_buddy.paths import data_dir
    return data_dir("agents")


def _load_confirmed() -> dict[str, str]:
    """Load persistent confirmed-working versions.

    Returns dict mapping plugin_id to version string.
    """
    path = _agents_dir() / _CONFIRMED_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_confirmed(confirmed: dict[str, str]) -> None:
    """Persist confirmed-working versions."""
    path = _agents_dir() / _CONFIRMED_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(confirmed, indent=2), encoding="utf-8")


def confirm_working(plugin_id: str, version: str) -> None:
    """Record that a plugin version has been confirmed working.

    Call this after a successful check_ready() for an integrated plugin.

    Args:
        plugin_id: The Obsidian plugin ID (e.g. "tag-wrangler").
        version: The version string that was confirmed working.
    """
    confirmed = _load_confirmed()
    old = confirmed.get(plugin_id)
    confirmed[plugin_id] = version
    _save_confirmed(confirmed)
    if old and old != version:
        logger.info("Plugin %s confirmed working: %s -> %s", plugin_id, old, version)
    else:
        logger.debug("Plugin %s confirmed working: %s", plugin_id, version)


def snapshot_versions() -> dict[str, Any]:
    """Snapshot current versions of ALL installed plugins.

    Returns a dict with:
    - timestamp: ISO 8601 snapshot time
    - plugins: dict mapping plugin_id to:
      - version: str
      - active: bool
      - name: str -- display name
      - integrated: bool -- whether work-buddy has a direct integration
      - integration: str | None -- work-buddy package (if integrated)
      - last_working_confirmed: str | None -- last version confirmed
        working via check_ready() (integrated plugins only)
    """
    vault = _vault_root()
    all_installed = installed_plugins(vault)
    all_active = active_plugins(vault)
    confirmed = _load_confirmed()

    plugins = {}
    for plugin_id, info in all_installed.items():
        is_integrated = plugin_id in INTEGRATED_PLUGINS
        entry = {
            "version": info.get("version", "unknown"),
            "active": plugin_id in all_active,
            "name": info.get("name", plugin_id),
            "integrated": is_integrated,
        }
        if is_integrated:
            entry["integration"] = INTEGRATED_PLUGINS[plugin_id]
            entry["last_working_confirmed"] = confirmed.get(plugin_id)
        plugins[plugin_id] = entry

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "plugin_count": len(plugins),
        "integrated_count": sum(1 for p in plugins.values() if p["integrated"]),
        "active_count": sum(1 for p in plugins.values() if p["active"]),
        "plugins": plugins,
    }


def save_snapshot(session_dir: Path | None = None) -> Path:
    """Snapshot versions and save to the agent session directory.

    Args:
        session_dir: Override session directory. If None, uses the
            current agent session dir.

    Returns the path to the saved snapshot file.
    """
    global _cached_snapshot

    if session_dir is None:
        from work_buddy.agent_session import get_session_dir
        session_dir = get_session_dir()

    snap = snapshot_versions()
    _cached_snapshot = snap

    out = session_dir / _SNAPSHOT_FILENAME
    out.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    logger.info("Plugin version snapshot saved to %s", out)
    return out


def get_snapshot(session_dir: Path | None = None) -> dict[str, Any]:
    """Get the current session's snapshot, creating it if needed.

    Lazy: only reads disk / snapshots once per session.
    """
    global _cached_snapshot
    if _cached_snapshot is not None:
        return _cached_snapshot

    if session_dir is None:
        from work_buddy.agent_session import get_session_dir
        session_dir = get_session_dir()

    snap_path = session_dir / _SNAPSHOT_FILENAME
    if snap_path.exists():
        _cached_snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
        return _cached_snapshot

    # First access — create the snapshot
    save_snapshot(session_dir)
    return _cached_snapshot


def diff_versions(old: dict, new: dict | None = None) -> list[dict]:
    """Compare two version snapshots and return changes.

    Args:
        old: Previous snapshot (e.g., from a prior session).
        new: Current snapshot. If None, takes a fresh one.

    Returns a list of change dicts for integrated plugins only:
    - plugin_id: str
    - old_version: str | None
    - new_version: str | None
    - change: "updated" | "added" | "removed" | "deactivated" | "activated"
    - integration: str
    - last_working_confirmed: str | None
    """
    if new is None:
        new = snapshot_versions()

    old_plugins = old.get("plugins", {})
    new_plugins = new.get("plugins", {})

    # Only diff integrated plugins — too noisy otherwise
    integrated_ids = set(INTEGRATED_PLUGINS.keys())
    all_ids = (set(old_plugins) | set(new_plugins)) & integrated_ids
    changes = []

    for pid in sorted(all_ids):
        old_info = old_plugins.get(pid)
        new_info = new_plugins.get(pid)

        if old_info and not new_info:
            changes.append({
                "plugin_id": pid,
                "old_version": old_info["version"],
                "new_version": None,
                "change": "removed",
                "integration": INTEGRATED_PLUGINS.get(pid, "unknown"),
                "last_working_confirmed": old_info.get("last_working_confirmed"),
            })
        elif not old_info and new_info:
            changes.append({
                "plugin_id": pid,
                "old_version": None,
                "new_version": new_info["version"],
                "change": "added",
                "integration": INTEGRATED_PLUGINS.get(pid, "unknown"),
                "last_working_confirmed": new_info.get("last_working_confirmed"),
            })
        elif old_info["version"] != new_info["version"]:
            changes.append({
                "plugin_id": pid,
                "old_version": old_info["version"],
                "new_version": new_info["version"],
                "change": "updated",
                "integration": INTEGRATED_PLUGINS.get(pid, "unknown"),
                "last_working_confirmed": new_info.get("last_working_confirmed"),
            })
        elif old_info.get("active") and not new_info.get("active"):
            changes.append({
                "plugin_id": pid,
                "old_version": old_info["version"],
                "new_version": new_info["version"],
                "change": "deactivated",
                "integration": INTEGRATED_PLUGINS.get(pid, "unknown"),
                "last_working_confirmed": new_info.get("last_working_confirmed"),
            })
        elif not old_info.get("active") and new_info.get("active"):
            changes.append({
                "plugin_id": pid,
                "old_version": old_info["version"],
                "new_version": new_info["version"],
                "change": "activated",
                "integration": INTEGRATED_PLUGINS.get(pid, "unknown"),
                "last_working_confirmed": new_info.get("last_working_confirmed"),
            })

    return changes


def format_versions_report(snap: dict | None = None) -> str:
    """Format a human-readable version report for context bundles.

    Args:
        snap: Snapshot to format. If None, takes a fresh one.
    """
    if snap is None:
        snap = snapshot_versions()

    plugins = snap.get("plugins", {})
    integrated = {k: v for k, v in plugins.items() if v.get("integrated")}
    other = {k: v for k, v in plugins.items() if not v.get("integrated")}

    lines = [
        f"## Plugin Versions ({snap.get('plugin_count', '?')} installed, "
        f"{snap.get('integrated_count', '?')} integrated, "
        f"{snap.get('active_count', '?')} active)",
        "",
        "### Integrated",
        "",
    ]

    for pid, info in sorted(integrated.items()):
        status = "active" if info.get("active") else "INACTIVE"
        confirmed = info.get("last_working_confirmed")
        ver = info["version"]
        if confirmed and confirmed != ver:
            ver_str = f"v{ver} (confirmed: v{confirmed})"
        elif confirmed:
            ver_str = f"v{ver} (confirmed)"
        else:
            ver_str = f"v{ver} (UNCONFIRMED)"
        lines.append(f"- **{info['name']}** {ver_str} ({status}) -- `{info.get('integration', '?')}`")

    lines += ["", "### Other", ""]
    for pid, info in sorted(other.items()):
        status = "active" if info.get("active") else "inactive"
        lines.append(f"- {info['name']} v{info['version']} ({status})")

    return "\n".join(lines)
