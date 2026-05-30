"""Internal transactional CRUD layer for the knowledge store.

Validated create / update / delete / move primitives for knowledge units. Each
unit is one Markdown file under ``knowledge/store/`` — editing a unit is editing
its file — and this module is the *transactional* API around the file-store seam
(``work_buddy/knowledge/file_store.py``): it adds path validation,
duplicate-placeholder rejection, DAG validation, and cache invalidation.

Surface: ``delete_unit`` / ``move_unit`` back the ``docs_delete`` / ``docs_move``
capabilities. ``create_unit`` / ``update_unit`` are the validated write
primitives. Content authoring and creation go through the ``docs_edit`` workflow
— the agent edits a unit's ``.md`` file directly and the commit step validates +
reconciles — so there is no ``docs_create`` / ``docs_update`` field-replacement
capability and no ``workflow_create`` / ``workflow_update`` (workflow units are
edited the same way, frontmatter ``steps`` and all).

``children`` is not authored or stored — a unit's children are derived at load
time from other units' ``parents`` — so no child-list reconciliation happens
here.
"""

from __future__ import annotations

from typing import Any

from work_buddy.knowledge import file_store
from work_buddy.knowledge.model import (
    _KIND_MAP,
    _PLACEHOLDER_RE,
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
# Store-mutation helpers
# ---------------------------------------------------------------------------

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
        slipped in via direct file edits or other bypass paths.

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
    store: dict[str, Any],
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
            "re-submit."
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
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    dev_notes: str | None = None,
    entry_points: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new unit in the knowledge store.

    Validates the path doesn't already exist, writes the unit's ``.md``
    file, validates the DAG, and invalidates the cache.
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
            for k in ("capability_name", "category", "parameters", "mutates_state",
                      "retry_policy", "consent_required", "consent_operations",
                      "op", "schema_version"):
                if k in extra:
                    unit_data[k] = extra[k]
    elif kind == "workflow":
        if extra:
            for k in ("workflow_name", "execution", "allow_override", "steps",
                      "step_instructions", "params_schema", "schema_version"):
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

    if extra and "requires" in extra:
        unit_data["requires"] = extra["requires"]

    # Write the unit file
    target_file = file_store.write_unit(_STORE_DIR, path, unit_data)

    # Validate
    errors = _invalidate_and_validate()
    hints = _scan_placeholder_hints(path, load_store())

    return {
        "status": "created",
        "path": path,
        "file": target_file.relative_to(_STORE_DIR).as_posix(),
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

    unit_data = file_store.read_unit(_STORE_DIR, path)
    if unit_data is None:
        return {"error": f"Path '{path}' has no unit file in the store."}

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

    target_file = file_store.write_unit(_STORE_DIR, path, unit_data)

    errors = _invalidate_and_validate()
    hints = _scan_placeholder_hints(path, load_store())

    return {
        "status": "updated",
        "path": path,
        "file": target_file.relative_to(_STORE_DIR).as_posix(),
        "updated_fields": all_updated_fields,
        "dag_errors": errors,
        "hints": hints,
    }


def delete_unit(path: str) -> dict[str, Any]:
    """Delete a unit from the store.

    Removes the unit's file and strips it from any child unit's ``parents``
    list so the DAG carries no dangling reference. Validates and invalidates.
    """
    store = load_store()

    if path not in store:
        return {"error": f"Path '{path}' not found in store."}

    unit = store[path]
    child_paths = list(unit.children)  # derived; snapshot before invalidation

    if not file_store.delete_unit(_STORE_DIR, path):
        return {"error": f"Path '{path}' has no unit file in the store."}

    # Strip the deleted path from each child's parents so no unit references
    # a parent that no longer exists.
    for child_path in child_paths:
        child_data = file_store.read_unit(_STORE_DIR, child_path)
        if child_data is None:
            continue
        parents = child_data.get("parents", [])
        if path in parents:
            child_data["parents"] = [p for p in parents if p != path]
            file_store.write_unit(_STORE_DIR, child_path, child_data)

    errors = _invalidate_and_validate()

    return {
        "status": "deleted",
        "path": path,
        "dag_errors": errors,
    }


def move_unit(old_path: str, new_path: str) -> dict[str, Any]:
    """Move a unit to a new path.

    Moves the unit's file and rewrites any other unit that names the old
    path as a parent.
    """
    store = load_store()

    if old_path not in store:
        return {"error": f"Path '{old_path}' not found in store."}
    if new_path in store:
        return {"error": f"Path '{new_path}' already exists."}
    if file_store.read_unit(_STORE_DIR, old_path) is None:
        return {"error": f"Path '{old_path}' has no unit file in the store."}

    file_store.move_unit(_STORE_DIR, old_path, new_path)
    _update_parent_references(old_path, new_path)

    errors = _invalidate_and_validate()

    return {
        "status": "moved",
        "old_path": old_path,
        "new_path": new_path,
        "dag_errors": errors,
    }


def _update_parent_references(old_path: str, new_path: str) -> None:
    """Rewrite every unit that names ``old_path`` as a parent to ``new_path``."""
    for unit_path in file_store.list_unit_paths(_STORE_DIR):
        unit_data = file_store.read_unit(_STORE_DIR, unit_path)
        if unit_data is None:
            continue
        parents = unit_data.get("parents", [])
        if old_path in parents:
            unit_data["parents"] = [
                new_path if p == old_path else p for p in parents
            ]
            file_store.write_unit(_STORE_DIR, unit_path, unit_data)


# ---------------------------------------------------------------------------
# MCP-facing callables
# ---------------------------------------------------------------------------

def docs_delete(*, path: str) -> dict[str, Any]:
    """Delete a unit from the knowledge store.

    Removes the unit and strips it from any child unit's ``parents``.

    Args:
        path: Path of unit to delete.
    """
    return delete_unit(path)


def docs_move(*, old_path: str, new_path: str) -> dict[str, Any]:
    """Move a unit to a new path in the knowledge store.

    Updates every unit that names the old path as a parent.

    Args:
        old_path: Current path of the unit.
        new_path: New path for the unit.
    """
    return move_unit(old_path, new_path)
