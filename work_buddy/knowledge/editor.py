"""Programmatic editor for the knowledge store.

Provides CRUD operations for PromptUnits: create, update, delete, move.
Every mutation validates the store and invalidates the cache.

All operations write to hand-authored JSON files in ``knowledge/store/``.
Generated files (``_generated_*.json``) are never touched — those are
rebuilt by ``build.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from work_buddy.knowledge.model import (
    PromptUnit,
    _KIND_MAP,
    unit_from_dict,
    validate_dag,
)
from work_buddy.knowledge.store import (
    _STORE_DIR,
    invalidate_store,
    load_store,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# File locators
# ---------------------------------------------------------------------------

def _find_file_for_path(unit_path: str) -> Path | None:
    """Find which JSON file contains a given unit path. Skips generated files."""
    for json_file in sorted(_STORE_DIR.glob("*.json")):
        if json_file.name.startswith("_generated_"):
            continue
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if unit_path in data:
            return json_file
    return None


def _best_file_for_new_path(unit_path: str) -> Path:
    """Choose the best JSON file to house a new unit based on path prefix.

    Heuristic: find the hand-authored file whose existing paths share the
    longest common prefix with the new path. Falls back to the top-level
    domain segment (e.g. "tasks/foo" → tasks.json).
    """
    top_segment = unit_path.split("/")[0]

    # Try exact domain file
    candidate = _STORE_DIR / f"{top_segment}.json"
    if candidate.exists() and not candidate.name.startswith("_generated_"):
        return candidate

    # Scan for best prefix match
    best_file: Path | None = None
    best_overlap = 0
    for json_file in sorted(_STORE_DIR.glob("*.json")):
        if json_file.name.startswith("_generated_"):
            continue
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for existing_path in data:
            overlap = len(_common_prefix(unit_path, existing_path))
            if overlap > best_overlap:
                best_overlap = overlap
                best_file = json_file

    if best_file:
        return best_file

    # Last resort: create new file for the domain
    return _STORE_DIR / f"{top_segment}.json"


def _common_prefix(a: str, b: str) -> str:
    """Common path prefix of two unit paths."""
    parts_a = a.split("/")
    parts_b = b.split("/")
    common = []
    for pa, pb in zip(parts_a, parts_b):
        if pa == pb:
            common.append(pa)
        else:
            break
    return "/".join(common)


def _read_json_file(path: Path) -> dict[str, Any]:
    """Read a JSON file, returning empty dict if not found."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    """Write JSON with consistent formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _invalidate_and_validate() -> list[str]:
    """Invalidate cache and run DAG validation. Returns errors."""
    invalidate_store()
    store = load_store(force=True)
    return validate_dag(store)


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

def create_unit(
    path: str,
    kind: str,
    name: str,
    description: str,
    *,
    content_full: str = "",
    content_summary: str = "",
    trigger: str = "",
    command: str | None = None,
    workflow: str | None = None,
    capabilities: list[str] | None = None,
    parents: list[str] | None = None,
    children: list[str] | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new unit in the knowledge store.

    Validates the path doesn't already exist, writes to the appropriate
    JSON file, updates parent children lists, validates DAG, and
    invalidates the cache.
    """
    store = load_store()

    if path in store:
        return {"error": f"Path '{path}' already exists. Use update instead."}

    if kind not in _KIND_MAP:
        return {"error": f"Invalid kind '{kind}'. Must be one of: {', '.join(_KIND_MAP)}"}

    # Build unit data
    unit_data: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "description": description,
    }
    if content_full or content_summary:
        unit_data["content"] = {}
        if content_full:
            unit_data["content"]["full"] = content_full
        if content_summary:
            unit_data["content"]["summary"] = content_summary or content_full[:200]
    if tags:
        unit_data["tags"] = tags
    if aliases:
        unit_data["aliases"] = aliases
    if parents:
        unit_data["parents"] = parents
    if children:
        unit_data["children"] = children

    # Kind-specific fields
    if kind == "directions":
        if trigger:
            unit_data["trigger"] = trigger
        if command:
            unit_data["command"] = command
        if workflow:
            unit_data["workflow"] = workflow
        if capabilities:
            unit_data["capabilities"] = capabilities
    elif kind == "capability":
        if extra:
            for k in ("capability_name", "category", "parameters", "mutates_state", "retry_policy", "consent_required"):
                if k in extra:
                    unit_data[k] = extra[k]
    elif kind == "workflow":
        if extra:
            for k in ("workflow_name", "execution", "allow_override", "steps", "step_instructions"):
                if k in extra:
                    unit_data[k] = extra[k]
        if command:
            unit_data["command"] = command
    elif kind == "system":
        if extra:
            for k in ("ports", "entry_points"):
                if k in extra:
                    unit_data[k] = extra[k]

    if extra:
        # Pass through requires
        if "requires" in extra:
            unit_data["requires"] = extra["requires"]

    # Determine target file and write
    target_file = _best_file_for_new_path(path)
    file_data = _read_json_file(target_file)
    file_data[path] = unit_data
    _write_json_file(target_file, file_data)

    # Update parent children lists
    _add_child_to_parents(path, parents or [])

    # Validate
    errors = _invalidate_and_validate()

    return {
        "status": "created",
        "path": path,
        "file": target_file.name,
        "dag_errors": errors,
    }


def update_unit(
    path: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Update fields on an existing unit.

    Supports deep merge for nested dicts (content, parameters).
    The ``content_full`` and ``content_summary`` shorthand keys
    are expanded into ``content.full`` and ``content.summary``.
    """
    store = load_store()

    if path not in store:
        return {"error": f"Path '{path}' not found in store."}

    target_file = _find_file_for_path(path)
    if target_file is None:
        return {"error": f"Path '{path}' exists in store but not in any hand-authored JSON file (may be generated)."}

    file_data = _read_json_file(target_file)
    if path not in file_data:
        return {"error": f"Path '{path}' not found in {target_file.name}."}

    unit_data = file_data[path]

    # Capture all field names before popping shorthand keys
    all_updated_fields = list(updates.keys())

    # Handle shorthand keys
    if "content_full" in updates:
        unit_data.setdefault("content", {})["full"] = updates.pop("content_full")
    if "content_summary" in updates:
        unit_data.setdefault("content", {})["summary"] = updates.pop("content_summary")

    # Deep merge remaining updates
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(unit_data.get(key), dict):
            unit_data[key].update(value)
        else:
            unit_data[key] = value

    file_data[path] = unit_data
    _write_json_file(target_file, file_data)

    errors = _invalidate_and_validate()

    return {
        "status": "updated",
        "path": path,
        "file": target_file.name,
        "updated_fields": all_updated_fields,
        "dag_errors": errors,
    }


def delete_unit(path: str) -> dict[str, Any]:
    """Delete a unit from the store.

    Removes parent/child references, cleans up the JSON file,
    validates DAG, and invalidates the cache.
    """
    store = load_store()

    if path not in store:
        return {"error": f"Path '{path}' not found in store."}

    target_file = _find_file_for_path(path)
    if target_file is None:
        return {"error": f"Path '{path}' exists in store but not in any hand-authored JSON file."}

    unit = store[path]

    # Remove from parent's children lists
    _remove_child_from_parents(path, unit.parents)

    # Remove from children's parent lists
    _remove_parent_from_children(path, unit.children)

    # Remove from file
    file_data = _read_json_file(target_file)
    file_data.pop(path, None)
    _write_json_file(target_file, file_data)

    errors = _invalidate_and_validate()

    return {
        "status": "deleted",
        "path": path,
        "file": target_file.name,
        "dag_errors": errors,
    }


def move_unit(old_path: str, new_path: str) -> dict[str, Any]:
    """Move a unit to a new path.

    Updates all parent/child references, moves the data in JSON files.
    """
    store = load_store()

    if old_path not in store:
        return {"error": f"Path '{old_path}' not found in store."}
    if new_path in store:
        return {"error": f"Path '{new_path}' already exists."}

    # Read current data
    source_file = _find_file_for_path(old_path)
    if source_file is None:
        return {"error": f"Path '{old_path}' not in any hand-authored JSON file."}

    source_data = _read_json_file(source_file)
    unit_data = source_data.pop(old_path)
    _write_json_file(source_file, source_data)

    # Write to destination file
    dest_file = _best_file_for_new_path(new_path)
    dest_data = _read_json_file(dest_file)
    dest_data[new_path] = unit_data
    _write_json_file(dest_file, dest_data)

    # Update references: any unit referencing old_path in parents/children
    _update_references(old_path, new_path)

    errors = _invalidate_and_validate()

    return {
        "status": "moved",
        "old_path": old_path,
        "new_path": new_path,
        "source_file": source_file.name,
        "dest_file": dest_file.name,
        "dag_errors": errors,
    }


# ---------------------------------------------------------------------------
# Reference management helpers
# ---------------------------------------------------------------------------

def _add_child_to_parents(child_path: str, parent_paths: list[str]) -> None:
    """Add child_path to each parent's children list in their JSON files."""
    for parent_path in parent_paths:
        parent_file = _find_file_for_path(parent_path)
        if parent_file is None:
            continue
        file_data = _read_json_file(parent_file)
        if parent_path not in file_data:
            continue
        children = file_data[parent_path].get("children", [])
        if child_path not in children:
            children.append(child_path)
            children.sort()
            file_data[parent_path]["children"] = children
            _write_json_file(parent_file, file_data)


def _remove_child_from_parents(child_path: str, parent_paths: list[str]) -> None:
    """Remove child_path from each parent's children list."""
    for parent_path in parent_paths:
        parent_file = _find_file_for_path(parent_path)
        if parent_file is None:
            continue
        file_data = _read_json_file(parent_file)
        if parent_path not in file_data:
            continue
        children = file_data[parent_path].get("children", [])
        if child_path in children:
            children.remove(child_path)
            file_data[parent_path]["children"] = children
            _write_json_file(parent_file, file_data)


def _remove_parent_from_children(parent_path: str, child_paths: list[str]) -> None:
    """Remove parent_path from each child's parents list."""
    for child_path in child_paths:
        child_file = _find_file_for_path(child_path)
        if child_file is None:
            continue
        file_data = _read_json_file(child_file)
        if child_path not in file_data:
            continue
        parents = file_data[child_path].get("parents", [])
        if parent_path in parents:
            parents.remove(parent_path)
            file_data[child_path]["parents"] = parents
            _write_json_file(child_file, file_data)


def _update_references(old_path: str, new_path: str) -> None:
    """Update all parent/child references from old_path to new_path across all files."""
    for json_file in sorted(_STORE_DIR.glob("*.json")):
        if json_file.name.startswith("_generated_"):
            continue
        try:
            file_data = _read_json_file(json_file)
        except (json.JSONDecodeError, OSError):
            continue

        changed = False
        for unit_path, unit_data in file_data.items():
            parents = unit_data.get("parents", [])
            if old_path in parents:
                parents[parents.index(old_path)] = new_path
                changed = True
            children = unit_data.get("children", [])
            if old_path in children:
                children[children.index(old_path)] = new_path
                changed = True

        if changed:
            _write_json_file(json_file, file_data)


# ---------------------------------------------------------------------------
# MCP-facing callables
# ---------------------------------------------------------------------------

def docs_create(
    *,
    path: str,
    kind: str,
    name: str,
    description: str,
    content_full: str = "",
    content_summary: str = "",
    trigger: str = "",
    command: str | None = None,
    workflow: str | None = None,
    capabilities: str | None = None,
    parents: str | None = None,
    children: str | None = None,
    tags: str | None = None,
    aliases: str | None = None,
) -> dict[str, Any]:
    """Create a new unit in the knowledge store.

    Args:
        path: Unique path ID (e.g. "tasks/my-directions").
        kind: Unit type: directions, system, capability, workflow.
        name: Human-readable name.
        description: One-line summary.
        content_full: Full content text. Accepts raw text (newlines preserved).
        content_summary: Short summary. Defaults to first 200 chars of content_full.
        trigger: (directions only) When to use this unit.
        command: (directions/workflow) Slash command name.
        workflow: (directions) Linked workflow path.
        capabilities: (directions) Comma-separated MCP capability paths.
        parents: Comma-separated parent paths.
        children: Comma-separated child paths.
        tags: Comma-separated search tags.
        aliases: Comma-separated search aliases.
    """
    return create_unit(
        path=path,
        kind=kind,
        name=name,
        description=description,
        content_full=content_full,
        content_summary=content_summary,
        trigger=trigger,
        command=command,
        workflow=workflow,
        capabilities=_split_csv(capabilities),
        parents=_split_csv(parents),
        children=_split_csv(children),
        tags=_split_csv(tags),
        aliases=_split_csv(aliases),
    )


def docs_update(
    *,
    path: str,
    name: str | None = None,
    description: str | None = None,
    content_full: str | None = None,
    content_summary: str | None = None,
    trigger: str | None = None,
    command: str | None = None,
    parents: str | None = None,
    children: str | None = None,
    tags: str | None = None,
    aliases: str | None = None,
) -> dict[str, Any]:
    """Update fields on an existing knowledge unit.

    Only provided fields are updated; omitted fields are unchanged.

    Args:
        path: Path of unit to update.
        name: New human-readable name.
        description: New one-line summary.
        content_full: New full content text.
        content_summary: New summary text.
        trigger: (directions) New trigger description.
        command: (directions/workflow) New slash command name.
        parents: New comma-separated parent paths (replaces existing).
        children: New comma-separated child paths (replaces existing).
        tags: New comma-separated tags (replaces existing).
        aliases: New comma-separated aliases (replaces existing).
    """
    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if content_full is not None:
        updates["content_full"] = content_full
    if content_summary is not None:
        updates["content_summary"] = content_summary
    if trigger is not None:
        updates["trigger"] = trigger
    if command is not None:
        updates["command"] = command
    if parents is not None:
        updates["parents"] = _split_csv(parents)
    if children is not None:
        updates["children"] = _split_csv(children)
    if tags is not None:
        updates["tags"] = _split_csv(tags)
    if aliases is not None:
        updates["aliases"] = _split_csv(aliases)

    if not updates:
        return {"error": "No fields to update."}

    return update_unit(path, updates)


def docs_delete(*, path: str) -> dict[str, Any]:
    """Delete a unit from the knowledge store.

    Removes the unit and cleans up parent/child references.

    Args:
        path: Path of unit to delete.
    """
    return delete_unit(path)


def docs_move(*, old_path: str, new_path: str) -> dict[str, Any]:
    """Move a unit to a new path in the knowledge store.

    Updates all parent/child references across the store.

    Args:
        old_path: Current path of the unit.
        new_path: New path for the unit.
    """
    return move_unit(old_path, new_path)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _split_csv(value: str | None) -> list[str] | None:
    """Split a comma-separated string into a list. Returns None if empty."""
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]
