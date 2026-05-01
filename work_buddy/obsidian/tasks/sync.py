"""Task file ↔ store synchronization.

The master task list is the **source of truth**. The store follows the file.

Compares the master task list (markdown file) against the SQLite metadata
store and auto-resolves discrepancies:

1. **Orphan in file**: Task has a 🆔 in the file but no store record.
   → Auto-creates a store record (state=inbox or done, urgency=medium).

2. **Orphan in store**: Store record exists but no matching task line in
   the file (manually deleted or moved).
   → Tombstone-deleted from the store.

3. **Checkbox mismatch**: File says done (``- [x]``) but store says
   non-done, or vice versa.
   → Store state updated to match the file.

Designed to run as a sidecar scheduled job (every 30 minutes). Uses the
Obsidian bridge when available, falls back to direct filesystem reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.obsidian.tasks import store
from work_buddy.obsidian.tasks.mutations import (
    MASTER_TASK_FILE,
    TASK_ID_RE,
    extract_description_from_line,
)

# Matches the task-note wikilink embedded in a task line, e.g. [[<uuid>|📓]].
# The 📓 alias keeps this distinct from ordinary wikilinks on the same line.
NOTE_WIKILINK_RE = re.compile(
    r"\[\[([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\|📓\]\]"
)

# Matches inline tags like `#paper/ecg-classifier` or `#health/sleep`.
# The lookbehind avoids matching `#` that sits inside a word (e.g., an ID or
# URL fragment). Nested paths (a/b/c) are allowed.
TAG_RE = re.compile(r"(?<![\w/])#([a-z0-9][a-z0-9_/-]*)", re.IGNORECASE)

# Tag prefixes that are never treated as user-defined namespaces.
#
# - `tasker/...`: legacy work-buddy metadata (being stripped elsewhere)
#
# Note: `projects/` is NOT reserved — `#projects/<slug>` is both the registry
# link AND a first-class organizational axis in the namespace tree. Keeping
# it out of the tree previously forced users to invent parallel taxonomies
# (e.g., `#research/<slug>`) just to surface tasks in the dashboard.
#
# Note: `wb/` is NOT reserved — it's the canonical work-buddy-dev namespace.
# Only the specific inline-todo markers `wb/todo` and `wb/done` are excluded
# (see RESERVED_TAG_EXACT).
RESERVED_TAG_PREFIXES: tuple[str, ...] = (
    "tasker/",
)

# Specific tag values that are reserved regardless of prefix. These are
# system markers (plugin-owned or inline-todo workflow) that would otherwise
# be mis-classified as namespaces.
RESERVED_TAG_EXACT: frozenset[str] = frozenset({
    "todo",
    "wb/todo",
    "wb/done",
})

# Tags starting with these prefixes are *always* namespacey, regardless of
# discovery frequency. They give power users an explicit opt-in.
NAMESPACE_OPT_IN_PREFIXES: tuple[str, ...] = ("ns/", "task/")

logger = get_logger(__name__)


def _is_reserved(tag: str) -> bool:
    """True if ``tag`` matches a reserved prefix or exact-value (never a
    user namespace)."""
    tag_lower = tag.lower()
    if tag_lower in RESERVED_TAG_EXACT:
        return True
    for prefix in RESERVED_TAG_PREFIXES:
        if tag_lower.startswith(prefix):
            return True
    return False


def _is_opt_in(tag: str) -> bool:
    """True if ``tag`` uses an always-namespacey opt-in prefix."""
    tag_lower = tag.lower()
    return any(tag_lower.startswith(p) for p in NAMESPACE_OPT_IN_PREFIXES)


def _namespace_threshold() -> int:
    """Minimum open-task count for a tag to be classified as a namespace."""
    cfg = load_config()
    val = cfg.get("tasks", {}).get("namespace_threshold", 2)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 2


def extract_tags_from_line(line: str) -> list[str]:
    """Pull all `#tag` tokens out of a task line, normalized (no leading '#').

    Preserves first-seen order; de-duplicates case-insensitively.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in TAG_RE.finditer(line):
        tag = m.group(1)
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def _tag_prefixes(tag: str) -> list[str]:
    """Return every ancestor prefix of a slash-separated tag, shallowest first.

    ``research/electricrag/writing-prep`` → ``["research", "research/electricrag",
    "research/electricrag/writing-prep"]``.
    """
    parts = [p for p in tag.lower().split("/") if p]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def classify_tags(
    prefix_counts: dict[str, int],
    tag_list_for_this_task: list[str],
    *,
    threshold: int = 2,
) -> list[tuple[str, bool]]:
    """Classify each tag on a single task as namespacey or not.

    A tag is namespacey iff:
      - it is NOT in the reserved-prefix blocklist, AND
      - it uses an opt-in prefix (ns/, task/), OR *any* of its ancestor
        prefixes (including itself) has >= ``threshold`` tasks carrying it
        or a descendant (per ``prefix_counts``).

    The ancestor walk is what rescues rare leaves: a one-off like
    ``research/electricrag/writing-prep`` inherits namespacey-ness from
    a popular parent like ``research/electricrag`` or ``research``, so a
    unique sub-bucket is not silently dropped from the tree.

    Reserved tags are still returned (so the cache can be queried for
    non-tree linkages) but with ``is_namespace=False``.

    Args:
        prefix_counts: Map of prefix -> count of distinct tasks whose
                       tags include that prefix *or any descendant of it*.
                       Computed once per sync across the whole vault.
        tag_list_for_this_task: Tags parsed from this task's line.
        threshold: Minimum count for discovery-based classification.
    """
    result: list[tuple[str, bool]] = []
    for tag in tag_list_for_this_task:
        if _is_reserved(tag):
            result.append((tag, False))
            continue
        if _is_opt_in(tag):
            result.append((tag, True))
            continue
        rescued = any(
            prefix_counts.get(p, 0) >= threshold for p in _tag_prefixes(tag)
        )
        result.append((tag, rescued))
    return result


def _read_master_list() -> str | None:
    """Read the master task list, preferring the bridge, falling back to fs."""
    # Try bridge first (keeps Obsidian's view consistent)
    try:
        from work_buddy.obsidian import bridge

        if bridge.is_available():
            content = bridge.read_file(MASTER_TASK_FILE)
            if content is not None:
                return content
    except Exception:
        pass

    # Fallback: direct filesystem read
    cfg = load_config()
    vault_root = cfg.get("vault_root", "")
    if not vault_root:
        return None

    fs_path = Path(vault_root) / MASTER_TASK_FILE
    if fs_path.exists():
        return fs_path.read_text(encoding="utf-8")

    return None


def _parse_file_tasks(content: str) -> dict[str, dict[str, Any]]:
    """Parse task lines from the master list into {task_id: info} dict.

    Only includes lines that have a 🆔 identifier.
    """
    tasks: dict[str, dict[str, Any]] = {}

    for i, line in enumerate(content.split("\n")):
        line_stripped = line.strip()
        if not line_stripped.startswith("- ["):
            continue

        m = TASK_ID_RE.search(line_stripped)
        if not m:
            continue

        task_id = m.group(1)
        is_done = line_stripped.startswith("- [x]")

        note_match = NOTE_WIKILINK_RE.search(line_stripped)
        note_uuid = note_match.group(1) if note_match else None

        raw_tags = extract_tags_from_line(line_stripped)
        description = extract_description_from_line(line_stripped)

        tasks[task_id] = {
            "line_number": i + 1,
            "is_done": is_done,
            "line": line_stripped,
            "note_uuid": note_uuid,
            "raw_tags": raw_tags,
            "description": description,
        }

    return tasks


def _rebuild_tag_cache(
    file_tasks: dict[str, dict[str, Any]],
    surviving_ids: set[str],
) -> int:
    """Rebuild the ``task_tags`` cache from parsed line data.

    Only tasks still present in both the file and store (``surviving_ids``)
    are written; tasks deleted this sync run are cleaned up separately
    via the FK cascade. Returns the number of tasks whose tag rows were
    (re)written.
    """
    threshold = _namespace_threshold()

    # Prefix frequency across all parsed tasks: for each tag, we credit every
    # ancestor prefix (so `research/electricrag/x` contributes one task-count
    # to `research`, `research/electricrag`, and `research/electricrag/x`).
    # Using a set per task avoids double-counting when two tags share a prefix
    # on the same task. This is what lets the classifier rescue rare leaves
    # whose parent prefix is popular.
    prefix_counts: dict[str, int] = {}
    for info in file_tasks.values():
        task_prefixes: set[str] = set()
        for tag in info.get("raw_tags", []):
            task_prefixes.update(_tag_prefixes(tag))
        for p in task_prefixes:
            prefix_counts[p] = prefix_counts.get(p, 0) + 1

    written = 0
    for task_id in surviving_ids:
        info = file_tasks.get(task_id)
        if not info:
            continue
        classified = classify_tags(
            prefix_counts,
            info.get("raw_tags", []),
            threshold=threshold,
        )
        try:
            store.set_task_tags(task_id, classified)
            written += 1
        except Exception as exc:
            logger.warning("task_sync: failed to write tag cache for %s: %s", task_id, exc)

    return written


def task_sync() -> dict[str, Any]:
    """Compare master task list against the SQLite store and reconcile.

    Returns a summary dict with counts and details of any discrepancies
    found and actions taken.
    """
    content = _read_master_list()
    if content is None:
        return {
            "status": "error",
            "error": "Could not read master task list (bridge unavailable, file not found)",
        }

    # Parse file tasks
    file_tasks = _parse_file_tasks(content)
    file_ids = set(file_tasks.keys())

    # Query all non-archived store records
    store_records = store.query(include_archived=False)
    store_ids = {r["task_id"] for r in store_records}
    store_by_id = {r["task_id"]: r for r in store_records}

    # --- Detect discrepancies ---

    # 1. In file but not in store (manually added or pre-store tasks)
    orphan_in_file = file_ids - store_ids
    created: list[str] = []
    for task_id in orphan_in_file:
        info = file_tasks[task_id]
        initial_state = "done" if info["is_done"] else "inbox"
        try:
            store.create(
                task_id=task_id,
                state=initial_state,
                urgency="medium",
                note_uuid=info.get("note_uuid"),
                description=info.get("description") or None,
            )
            created.append(task_id)
            logger.info(
                "task_sync: created store record for %s (state=%s, line=%d, note_uuid=%s)",
                task_id, initial_state, info["line_number"], info.get("note_uuid"),
            )
        except Exception as exc:
            logger.warning("task_sync: failed to create store for %s: %s", task_id, exc)

    # 2. In store but not in file → tombstone-delete from store
    #    The file is the source of truth: if it's gone from the file, it's gone.
    deleted_from_store: list[str] = []
    for task_id in store_ids - file_ids:
        record = store_by_id[task_id]
        try:
            store.delete(task_id)
            deleted_from_store.append(task_id)
            logger.info(
                "task_sync: tombstone-deleted orphan %s (was state=%s)",
                task_id, record["state"],
            )
        except Exception as exc:
            logger.warning("task_sync: failed to delete orphan %s: %s", task_id, exc)

    # 3. Checkbox state mismatches → update store to match file
    resolved_mismatches: list[dict[str, Any]] = []
    for task_id in file_ids & store_ids:
        file_info = file_tasks[task_id]
        store_record = store_by_id[task_id]

        file_done = file_info["is_done"]
        store_done = store_record["state"] == "done"

        if file_done != store_done:
            new_state = "done" if file_done else "inbox"
            try:
                store.update(
                    task_id,
                    state=new_state,
                    reason=f"task_sync: file checkbox → {new_state}",
                )
                resolved_mismatches.append({
                    "task_id": task_id,
                    "old_store_state": store_record["state"],
                    "new_store_state": new_state,
                    "line_number": file_info["line_number"],
                })
                logger.info(
                    "task_sync: resolved mismatch %s — store %s → %s (line %d)",
                    task_id, store_record["state"], new_state,
                    file_info["line_number"],
                )
            except Exception as exc:
                logger.warning(
                    "task_sync: failed to resolve mismatch %s: %s", task_id, exc,
                )

    # 4. note_uuid drift → file is source of truth; backfill or correct the store.
    #    Only propagates a non-null file value. We deliberately do NOT clear the
    #    store's note_uuid when the file lacks the wikilink: a line can lose its
    #    emoji without the underlying note file being deleted, and we don't want
    #    to strand orphan notes in the vault by clearing pointers on a whim.
    resolved_note_uuids: list[dict[str, Any]] = []
    for task_id in file_ids & store_ids:
        file_info = file_tasks[task_id]
        store_record = store_by_id[task_id]

        file_note_uuid = file_info.get("note_uuid")
        store_note_uuid = store_record.get("note_uuid")

        if file_note_uuid and file_note_uuid != store_note_uuid:
            try:
                store.update(
                    task_id,
                    note_uuid=file_note_uuid,
                    reason=f"task_sync: note_uuid backfilled from file",
                )
                resolved_note_uuids.append({
                    "task_id": task_id,
                    "old_note_uuid": store_note_uuid,
                    "new_note_uuid": file_note_uuid,
                    "line_number": file_info["line_number"],
                })
                logger.info(
                    "task_sync: reconciled note_uuid %s -- store %s -> %s (line %d)",
                    task_id, store_note_uuid, file_note_uuid,
                    file_info["line_number"],
                )
            except Exception as exc:
                logger.warning(
                    "task_sync: failed to reconcile note_uuid %s: %s", task_id, exc,
                )

    # 5. description drift → file is source of truth.
    #    Backfills NULL descriptions for legacy rows AND updates rows
    #    whose stored description has drifted from the file (e.g. user
    #    manually edited the task text in Obsidian). Empty-string
    #    descriptions are skipped — those represent task lines we
    #    couldn't extract text from (malformed, all-emoji, etc.) and
    #    we'd rather keep the previous value than overwrite with empty.
    resolved_descriptions: list[dict[str, Any]] = []
    for task_id in file_ids & store_ids:
        file_info = file_tasks[task_id]
        store_record = store_by_id[task_id]

        file_desc = file_info.get("description") or ""
        store_desc = store_record.get("description")

        # Only act when the file has a real description AND it differs
        # from the store. NULL → file value is a backfill; non-NULL
        # mismatch is a drift correction.
        if file_desc and file_desc != store_desc:
            try:
                store.update(
                    task_id,
                    description=file_desc,
                    reason="task_sync: description drift from file",
                )
                resolved_descriptions.append({
                    "task_id": task_id,
                    "old_description": store_desc,
                    "new_description": file_desc,
                    "line_number": file_info["line_number"],
                })
                logger.info(
                    "task_sync: reconciled description %s — line %d",
                    task_id, file_info["line_number"],
                )
            except Exception as exc:
                logger.warning(
                    "task_sync: failed to reconcile description %s: %s",
                    task_id, exc,
                )

    # --- Tag cache rebuild ---
    # Survivors: everything in the file that also has (or now has) a store
    # record. Excludes records just tombstone-deleted.
    surviving_ids = file_ids & (store_ids | set(created))
    try:
        tag_rows_written = _rebuild_tag_cache(file_tasks, surviving_ids)
    except Exception as exc:
        logger.warning("task_sync: tag cache rebuild failed: %s", exc)
        tag_rows_written = 0

    # --- Slice 7: action-items markdown -> table reconciliation ---
    # Walks every developed/dense task with a note_uuid and runs
    # parse_action_items_from_note + reconcile_from_markdown.  Sparse
    # tasks are skipped (the explicit-sparsity doctrine -- empty
    # `## Action items` is meaningful, the agent shouldn't auto-fill).
    # Bridge unavailability is non-fatal; the next sync pass catches up.
    action_items_summary = {
        "tasks_examined": 0, "added": 0, "updated": 0,
        "deleted": 0, "kept": 0,
    }
    try:
        from work_buddy.obsidian import bridge as _bridge
        from work_buddy.obsidian.tasks import action_items as _ai

        for task_id in surviving_ids:
            store_row = store_by_id.get(task_id)
            if store_row is None:
                continue
            density = store_row.get("density") or "sparse"
            if density == "sparse":
                continue
            note_uuid = store_row.get("note_uuid")
            if not note_uuid:
                continue
            note_path = f"tasks/notes/{note_uuid}.md"
            try:
                body = _bridge.read_file(note_path)
            except Exception as exc:
                logger.debug(
                    "task_sync: action-items read failed for %s: %s",
                    task_id, exc,
                )
                continue
            if body is None:
                continue
            try:
                summary = _ai.reconcile_from_markdown(task_id, body)
            except Exception as exc:
                logger.warning(
                    "task_sync: action-items reconcile failed for %s: %s",
                    task_id, exc,
                )
                continue
            action_items_summary["tasks_examined"] += 1
            for k in ("added", "updated", "deleted", "kept"):
                action_items_summary[k] += summary.get(k, 0)
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("task_sync: action-items pass skipped: %s", exc)

    # --- Summary ---
    total_actions = (
        len(created)
        + len(deleted_from_store)
        + len(resolved_mismatches)
        + len(resolved_note_uuids)
        + len(resolved_descriptions)
    )
    status = "ok" if total_actions == 0 else "synced"

    result: dict[str, Any] = {
        "status": status,
        "file_tasks": len(file_ids),
        "store_records": len(store_ids),
        "created": len(created),
        "deleted": len(deleted_from_store),
        "resolved_mismatches": len(resolved_mismatches),
        "resolved_note_uuids": len(resolved_note_uuids),
        "resolved_descriptions": len(resolved_descriptions),
        "tag_rows_written": tag_rows_written,
        "action_items": action_items_summary,
    }

    # Include details only if actions were taken (keeps log concise)
    if created:
        result["created_details"] = created
    if deleted_from_store:
        result["deleted_details"] = deleted_from_store
    if resolved_mismatches:
        result["mismatch_details"] = resolved_mismatches
    if resolved_note_uuids:
        result["note_uuid_details"] = resolved_note_uuids
    if resolved_descriptions:
        result["description_details"] = resolved_descriptions

    if total_actions > 0:
        logger.info(
            "task_sync: %d actions — %d created, %d deleted, "
            "%d mismatches resolved, %d note_uuids reconciled, "
            "%d descriptions reconciled",
            total_actions, len(created), len(deleted_from_store),
            len(resolved_mismatches), len(resolved_note_uuids),
            len(resolved_descriptions),
        )
    else:
        logger.debug("task_sync: all clean (%d file, %d store)", len(file_ids), len(store_ids))

    return result
