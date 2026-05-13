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
    _PLACEHOLDER_RE,
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


def _best_file_for_new_path(unit_path: str, kind: str | None = None) -> Path:
    """Choose the best JSON file to house a new unit based on path prefix.

    Heuristic:
    - ``kind == "workflow"`` → always ``workflows.json`` (convention; the
      conductor scans all files by kind, but colocating workflows keeps
      hand-authoring consistent).
    - Otherwise: prefer the hand-authored file whose existing paths share
      the longest common prefix with the new path; fall back to the top-
      level domain segment (e.g. ``tasks/foo`` → ``tasks.json``).
    """
    if kind == "workflow":
        return _STORE_DIR / "workflows.json"

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


def _extract_placeholder_targets(text: str) -> list[str]:
    """Return every placeholder target path in *text*, in order.

    Duplicates appear in the returned list as many times as they
    appear in the source text. Empty / malformed placeholders are
    dropped. Used by both the editor pre-write check and the
    validator corpus walk.
    """
    targets: list[str] = []
    if "<<wb:" not in text:
        return targets
    for match in _PLACEHOLDER_RE.finditer(text):
        inner = match.group(1).strip()
        parts = inner.split()
        if not parts:
            continue
        target_path = parts[0]
        if target_path:
            targets.append(target_path)
    return targets


def check_duplicate_placeholders(content_full: str) -> list[dict[str, Any]]:
    """Find every target path duplicated in *content_full*.

    Returns a list of ``{"placeholder": <path>, "count": <int>}`` —
    one entry per duplicated target. Empty list means no duplicates.

    This is a HARD-ERROR check, not a hint. There is no legitimate
    reason to reference the same target twice within a unit: at read
    time the per-unit-occurrence cap turns every reference after the
    first into a back-reference marker, so duplicates produce no
    new content and only cost confusion.

    Used by:
      * Editor pre-write (``update_unit`` / ``create_unit``) — to
        reject duplicate-bearing edits before they land on disk.
      * Validator (``docs_validate``) — to surface duplicates that
        slipped in via direct JSON edits or other bypass paths.

    The function operates on a raw string so the editor can call it
    against a *proposed* content_full (not yet written to disk) and
    the validator can call it against each stored unit's content.
    """
    from collections import Counter
    targets = _extract_placeholder_targets(content_full)
    counts = Counter(targets)
    return [
        {"placeholder": path, "count": count}
        for path, count in counts.items()
        if count > 1
    ]


def _scan_placeholder_hints(
    unit_path: str,
    store: dict[str, PromptUnit],
) -> list[dict[str, str]]:
    """Authoring-time lint for the placeholder-recursion foot-gun.

    Flags plain ``<<wb:Y>>`` placeholders in the just-edited unit when
    Y's own ``content["full"]`` contains placeholder markup. In that
    situation the resolver inserts Y's body verbatim — the nested
    placeholders are left literal, and the reader never sees Y's
    foundations. Adding ``--recursive`` on the outer reference is
    usually what the author meant.

    Hints are informational only: the author may have deliberately
    chosen the shallow form, so this never blocks an edit. The result
    rides alongside ``dag_errors`` in the editor's response so the
    authoring agent sees it at the moment of writing, not at some
    later validate pass.

    Note: duplicate-placeholder detection is NOT a hint — it's a
    hard error in the editor and a validator check. See
    ``check_duplicate_placeholders``.

    Mirrors the corpus-wide audit in
    ``scripts/audit_placeholder_recursion.py``; this is the
    single-unit, write-time version.
    """
    unit = store.get(unit_path)
    if unit is None:
        return []
    full = unit.content.get("full", "")
    if "<<wb:" not in full:
        return []

    hints: list[dict[str, str]] = []
    seen_targets: set[str] = set()

    for match in _PLACEHOLDER_RE.finditer(full):
        inner = match.group(1).strip()
        parts = inner.split()
        if not parts:
            continue
        target_path = parts[0]
        if not target_path:
            continue
        if "--recursive" in parts[1:]:
            # Author already opted in to transitive resolution.
            continue
        if target_path in seen_targets:
            continue

        target = store.get(target_path)
        if target is None:
            # Broken refs aren't this lint's problem — the resolver's
            # not-found comment surfaces them downstream.
            continue
        if "<<wb:" not in target.content.get("full", ""):
            continue

        seen_targets.add(target_path)
        hints.append({
            "hint": "placeholder_recursion",
            "placeholder": target_path,
            "message": (
                f"Plain placeholder targets {target_path!r}, which itself "
                "contains placeholders. Add --recursive if you want them "
                "expanded; leave it plain if you only want this unit's "
                "raw body inlined."
            ),
        })

    return hints


def _duplicate_error_response(
    path: str,
    duplicates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the editor's rejection payload for duplicate placeholders.

    Surfaces every duplicated target with its count so the author
    knows exactly what to remove. Returned in place of the normal
    success dict so the write does NOT land on disk.
    """
    details = "; ".join(
        f"{d['placeholder']!r} appears {d['count']} times"
        for d in duplicates
    )
    return {
        "error": "placeholder_duplicate",
        "path": path,
        "duplicates": duplicates,
        "message": (
            f"Rejected: {details}. Each placeholder may appear at most "
            "once per unit — at read time the per-unit-occurrence cap "
            "renders subsequent references as back-reference markers, "
            "so duplicates add zero content. Remove the extras and "
            "re-submit. (When the materialization workflow lands, "
            "the buffer file will persist on rejection so you can "
            "fix in place without re-sending the whole payload.)"
        ),
    }


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
    dev_notes: str | None = None,
    entry_points: list[str] | None = None,
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

    # Hard reject: duplicate placeholders are not a legitimate
    # authorial choice. Check BEFORE writing to disk so the unit
    # never lands in a broken state.
    duplicates = check_duplicate_placeholders(content_full or "")
    if duplicates:
        return _duplicate_error_response(path, duplicates)

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
    # Universal: dev_notes surfaces in dev mode regardless of kind.
    # entry_points is system-kind metadata but we accept it here as a
    # first-class param so callers don't need to use the generic ``extra``.
    if dev_notes:
        unit_data["dev_notes"] = dev_notes
    if entry_points:
        unit_data["entry_points"] = entry_points

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
            for k in ("workflow_name", "execution", "allow_override", "steps", "step_instructions", "params_schema"):
                if k in extra:
                    unit_data[k] = extra[k]
        if command:
            unit_data["command"] = command
    elif kind == "service":
        if extra:
            for k in ("ports", "health_url", "entry_points"):
                if k in extra:
                    unit_data[k] = extra[k]
    elif kind == "integration":
        if extra:
            for k in ("external_system", "bridge_module", "ports", "entry_points"):
                if k in extra:
                    unit_data[k] = extra[k]
    elif kind == "reference":
        if extra and "entry_points" in extra:
            unit_data["entry_points"] = extra["entry_points"]

    if extra:
        # Pass through requires
        if "requires" in extra:
            unit_data["requires"] = extra["requires"]

    # Determine target file and write
    target_file = _best_file_for_new_path(path, kind=kind)
    file_data = _read_json_file(target_file)
    file_data[path] = unit_data
    _write_json_file(target_file, file_data)

    # Update parent children lists
    _add_child_to_parents(path, parents or [])

    # Validate
    errors = _invalidate_and_validate()
    hints = _scan_placeholder_hints(path, load_store())

    return {
        "status": "created",
        "path": path,
        "file": target_file.name,
        "dag_errors": errors,
        "hints": hints,
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

    # Hard reject: duplicate placeholders are not a legitimate
    # authorial choice. Compute the post-update content_full and
    # check BEFORE writing to disk. Two cases:
    #   1. Update touches content_full → check the proposed value.
    #   2. Update doesn't touch content_full → no new duplicates
    #      can appear from this edit; skip the check (the validator
    #      catches any pre-existing duplicates separately).
    proposed_full: str | None = None
    if "content_full" in updates:
        proposed_full = updates["content_full"]
    elif "content" in updates and isinstance(updates["content"], dict):
        # Deep-merge case: caller passed {"content": {"full": "..."}}.
        if "full" in updates["content"]:
            proposed_full = updates["content"]["full"]
    if proposed_full is not None:
        duplicates = check_duplicate_placeholders(proposed_full)
        if duplicates:
            return _duplicate_error_response(path, duplicates)

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
    hints = _scan_placeholder_hints(path, load_store())

    return {
        "status": "updated",
        "path": path,
        "file": target_file.name,
        "updated_fields": all_updated_fields,
        "dag_errors": errors,
        "hints": hints,
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
    dev_notes: str | None = None,
    entry_points: str | None = None,
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
        dev_notes=dev_notes if dev_notes else None,
        entry_points=_split_csv(entry_points),
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
    dev_notes: str | None = None,
    entry_points: str | None = None,
    kind: str | None = None,
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
        kind: New kind. Must be a registered kind (see ``_KIND_MAP``).
            Use sparingly — kind changes are reclassifications, not edits.
    """
    if kind is not None and kind not in _KIND_MAP:
        return {
            "error": f"Unknown kind {kind!r}. Valid kinds: {sorted(_KIND_MAP)}",
        }

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
    if dev_notes is not None:
        updates["dev_notes"] = dev_notes
    if entry_points is not None:
        updates["entry_points"] = _split_csv(entry_points)
    if kind is not None:
        updates["kind"] = kind

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
# Workflow-specific authoring
# ---------------------------------------------------------------------------
#
# ``docs_create`` / ``docs_update`` handle prose-shaped units (directions,
# system). Workflow units are structurally different: they carry a DAG
# (``steps``), per-step prose (``step_instructions``), and a few workflow-
# level knobs (``workflow_name``, ``execution``, ``allow_override``). Packing
# those into the prose ``docs_*`` schema would mix concerns — so workflow
# authoring lives in its own pair of capabilities.
#
# The ``steps`` and ``step_instructions`` parameters are accepted as JSON
# strings (parsed here) so the MCP transport can stay flat-typed; callers
# can still pass dicts when invoking the Python function directly.

_WORKFLOW_EXTRA_KEYS = ("workflow_name", "execution", "allow_override", "steps", "step_instructions", "params_schema")


def _coerce_json(value: Any, label: str) -> Any:
    """Accept a JSON string or already-parsed value; raise on bad JSON."""
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc


def workflow_create(
    *,
    path: str,
    name: str,
    description: str,
    workflow_name: str,
    steps: Any,
    step_instructions: Any = None,
    execution: str = "main",
    allow_override: bool = False,
    content_full: str = "",
    content_summary: str = "",
    command: str | None = None,
    parents: str | None = None,
    children: str | None = None,
    tags: str | None = None,
    aliases: str | None = None,
    dev_notes: str | None = None,
    params_schema: Any = None,
) -> dict[str, Any]:
    """Create a new workflow unit in the knowledge store.

    Workflow DAGs are structurally richer than prose units, so they have
    a dedicated creator. Prefer this over ``docs_create`` for ``kind="workflow"``
    units; ``docs_create`` does not accept workflow-specific fields.

    Args:
        path: Unique path ID (e.g. ``"dev/dev-document"``).
        name: Human-readable name.
        description: One-line summary.
        workflow_name: Registry slug used with ``wb_run("<workflow_name>")``.
        steps: DAG definition. Either a list of step dicts, or a JSON string
               encoding the same. Each step needs at least ``id``, ``name``,
               ``step_type`` (``"reasoning"`` | ``"code"``), and
               ``depends_on`` (list of prior step ids).
        step_instructions: Optional ``{step_id: instruction_text}`` mapping
               (dict or JSON string). Reasoning steps generally want this;
               pure auto_run steps usually don't need it.
        execution: Default execution policy (``"main"`` or ``"subagent"``).
        allow_override: Whether callers may override execution per step.
        content_full: Optional workflow-level context (philosophy, what-not-
               to-do). Surfaces at ``depth="full"`` on ``agent_docs``.
        content_summary: Optional one-paragraph summary.
        command: Slash-command name (e.g. ``"wb-dev-document"``) for routing.
        parents: Comma-separated parent paths (typical: the domain, e.g. ``"dev"``).
        children: Comma-separated child paths (usually empty).
        tags: Comma-separated search tags.
        aliases: Comma-separated search aliases.
        dev_notes: Dev-mode-only notes about the workflow's internals.

    Returns:
        The ``create_unit`` result dict (``{status, path, file, dag_errors}``)
        or ``{"error": ...}`` on malformed input.
    """
    try:
        steps_parsed = _coerce_json(steps, "steps")
        instructions_parsed = _coerce_json(step_instructions, "step_instructions")
        params_schema_parsed = _coerce_json(params_schema, "params_schema")
    except ValueError as exc:
        return {"error": str(exc)}

    if not isinstance(steps_parsed, list) or not steps_parsed:
        return {"error": "steps must be a non-empty list of step dicts"}
    if instructions_parsed is not None and not isinstance(instructions_parsed, dict):
        return {"error": "step_instructions must be a dict keyed by step id"}
    if params_schema_parsed is not None and not isinstance(params_schema_parsed, dict):
        return {"error": "params_schema must be a dict keyed by param name"}

    extra: dict[str, Any] = {
        "workflow_name": workflow_name,
        "execution": execution,
        "allow_override": bool(allow_override),
        "steps": steps_parsed,
    }
    if instructions_parsed:
        extra["step_instructions"] = instructions_parsed
    if params_schema_parsed:
        extra["params_schema"] = params_schema_parsed

    return create_unit(
        path=path,
        kind="workflow",
        name=name,
        description=description,
        content_full=content_full,
        content_summary=content_summary,
        command=command,
        parents=_split_csv(parents),
        children=_split_csv(children),
        tags=_split_csv(tags),
        aliases=_split_csv(aliases),
        dev_notes=dev_notes if dev_notes else None,
        extra=extra,
    )


def workflow_update(
    *,
    path: str,
    name: str | None = None,
    description: str | None = None,
    workflow_name: str | None = None,
    steps: Any = None,
    step_instructions: Any = None,
    execution: str | None = None,
    allow_override: bool | None = None,
    content_full: str | None = None,
    content_summary: str | None = None,
    command: str | None = None,
    parents: str | None = None,
    children: str | None = None,
    tags: str | None = None,
    aliases: str | None = None,
    dev_notes: str | None = None,
    params_schema: Any = None,
) -> dict[str, Any]:
    """Update an existing workflow unit.

    Only provided fields change; omitted fields preserved. For ``steps``
    and ``step_instructions``, the new value replaces the old entirely —
    partial per-step edits are not supported here (read the current value
    via ``agent_docs``, mutate, pass the whole structure back).

    Returns:
        The ``update_unit`` result dict or ``{"error": ...}`` on bad input.
    """
    try:
        steps_parsed = _coerce_json(steps, "steps") if steps is not None else None
        instructions_parsed = (
            _coerce_json(step_instructions, "step_instructions")
            if step_instructions is not None
            else None
        )
        params_schema_parsed = (
            _coerce_json(params_schema, "params_schema")
            if params_schema is not None
            else None
        )
    except ValueError as exc:
        return {"error": str(exc)}

    if steps_parsed is not None and (not isinstance(steps_parsed, list) or not steps_parsed):
        return {"error": "steps must be a non-empty list of step dicts"}
    if instructions_parsed is not None and not isinstance(instructions_parsed, dict):
        return {"error": "step_instructions must be a dict keyed by step id"}
    if params_schema_parsed is not None and not isinstance(params_schema_parsed, dict):
        return {"error": "params_schema must be a dict keyed by param name"}

    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if content_full is not None:
        updates["content_full"] = content_full
    if content_summary is not None:
        updates["content_summary"] = content_summary
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
    if dev_notes is not None:
        updates["dev_notes"] = dev_notes
    if workflow_name is not None:
        updates["workflow_name"] = workflow_name
    if execution is not None:
        updates["execution"] = execution
    if allow_override is not None:
        updates["allow_override"] = bool(allow_override)
    if steps_parsed is not None:
        updates["steps"] = steps_parsed
    if instructions_parsed is not None:
        updates["step_instructions"] = instructions_parsed
    if params_schema_parsed is not None:
        updates["params_schema"] = params_schema_parsed

    if not updates:
        return {"error": "No fields to update."}

    return update_unit(path, updates)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _split_csv(value: str | None) -> list[str] | None:
    """Split a comma-separated string into a list. Returns None if empty."""
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]
