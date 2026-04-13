"""Obsidian plugin detection and capability gating."""

import json
import functools
from pathlib import Path
from typing import Any, Callable


def _obsidian_dir(vault_root: Path) -> Path:
    return vault_root / ".obsidian"


def active_plugins(vault_root: Path) -> set[str]:
    """Return the set of currently enabled community plugin IDs."""
    cp_file = _obsidian_dir(vault_root) / "community-plugins.json"
    if not cp_file.exists():
        return set()
    with open(cp_file, encoding="utf-8") as f:
        data = json.load(f)
    return set(data) if isinstance(data, list) else set()


def is_active(vault_root: Path, plugin_id: str) -> bool:
    """Check if a specific plugin is currently enabled."""
    return plugin_id in active_plugins(vault_root)


def installed_plugins(vault_root: Path) -> dict[str, dict]:
    """Return {plugin_id: manifest_info} for all installed plugins.

    Reads manifest.json from each plugin directory.
    """
    plugins_dir = _obsidian_dir(vault_root) / "plugins"
    if not plugins_dir.is_dir():
        return {}

    result = {}
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "manifest.json"
        if manifest.exists():
            try:
                with open(manifest, encoding="utf-8") as f:
                    info = json.load(f)
                result[entry.name] = {
                    "id": info.get("id", entry.name),
                    "name": info.get("name", entry.name),
                    "version": info.get("version", "unknown"),
                    "description": info.get("description", ""),
                }
            except (json.JSONDecodeError, OSError):
                result[entry.name] = {"id": entry.name, "name": entry.name}

    return result


def plugin_config(vault_root: Path, plugin_id: str) -> dict[str, Any]:
    """Read a plugin's data.json configuration.

    Returns empty dict if plugin not found or config unreadable.
    """
    data_file = _obsidian_dir(vault_root) / "plugins" / plugin_id / "data.json"
    if not data_file.exists():
        return {}
    try:
        with open(data_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def require_plugins(*plugin_ids: str):
    """Decorator that raises RuntimeError if required plugins aren't active.

    Usage:
        @require_plugins("dataview", "dataview-serializer")
        def write_dataview_query(vault_root, ...):
            ...

    The decorated function's first positional argument must be vault_root (Path).
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(vault_root: Path, *args, **kwargs):
            active = active_plugins(vault_root)
            missing = set(plugin_ids) - active
            if missing:
                raise RuntimeError(
                    f"{fn.__name__} requires Obsidian plugins: {', '.join(sorted(missing))}. "
                    f"Enable them in Obsidian settings."
                )
            return fn(vault_root, *args, **kwargs)
        return wrapper
    return decorator
