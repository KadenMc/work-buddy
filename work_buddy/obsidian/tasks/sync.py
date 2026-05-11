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
    DONE_DATE_RE,
    DUE_DATE_RE,
    MASTER_TASK_FILE,
    TASK_ID_RE,
    URGENCY_EMOJI_RE,
    extract_description_from_line,
)


# Plugin-emoji → SQLite-column mapping.
#
# The Obsidian Tasks plugin owns the markdown emoji syntax; the
# task_metadata schema has the matching columns but the parser used to
# ignore them, leaving the bridge half-built. Mapping each glyph to a
# canonical urgency level lets task_sync drift-reconcile in both
# directions (📅 file → deadline_date, ⏫🔼🔽 file → urgency, ✅ file →
# completed_at). Adding a new priority emoji means updating this map +
# the URGENCY_EMOJI_RE in mutations.py.
_URGENCY_EMOJI_TO_LEVEL = {
    "⏫": "high",
    "🔼": "medium",
    "🔽": "low",
}

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

    Extracted fields:

    - ``is_done`` / ``note_uuid`` / ``raw_tags`` / ``description`` — the
      pre-Slice-N basics; the reconciliation loop has always tracked these.
    - ``deadline_date`` — ISO date from ``📅 YYYY-MM-DD`` (or ``None``).
    - ``urgency`` — ``"high"`` / ``"medium"`` / ``"low"`` from
      ``⏫`` / ``🔼`` / ``🔽`` (or ``None`` when no urgency emoji is present).
    - ``completed_at`` — ISO date from ``✅ YYYY-MM-DD`` (or ``None``).

    Emoji extraction here mirrors the columns the store carries for
    deadline / urgency / completed_at. The drift loops in ``task_sync``
    reconcile these parsed values into the canonical SQLite columns.
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

        # Plugin-emoji extraction. Each match yields just the date or the
        # glyph; we strip the leading emoji + whitespace so the column
        # stores the bare ISO date or canonical urgency level.
        due_match = DUE_DATE_RE.search(line_stripped)
        deadline_date = (
            due_match.group().replace("📅", "").strip() if due_match else None
        )

        done_match = DONE_DATE_RE.search(line_stripped)
        completed_at = (
            done_match.group().replace("✅", "").strip() if done_match else None
        )

        urgency_match = URGENCY_EMOJI_RE.search(line_stripped)
        urgency = (
            _URGENCY_EMOJI_TO_LEVEL.get(urgency_match.group())
            if urgency_match
            else None
        )

        tasks[task_id] = {
            "line_number": i + 1,
            "is_done": is_done,
            "line": line_stripped,
            "note_uuid": note_uuid,
            "raw_tags": raw_tags,
            "description": description,
            "deadline_date": deadline_date,
            "urgency": urgency,
            "completed_at": completed_at,
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
        # Carry forward whatever emoji metadata the line already
        # encodes. Urgency falls back to "medium" only when no priority
        # emoji is present — previously this was hardcoded regardless
        # of what the file said, which broke drift detection for any
        # legacy line that already carried a 🔼/⏫/🔽.
        initial_urgency = info.get("urgency") or "medium"
        deadline_date = info.get("deadline_date")
        try:
            store.create(
                task_id=task_id,
                state=initial_state,
                urgency=initial_urgency,
                note_uuid=info.get("note_uuid"),
                description=info.get("description") or None,
                has_deadline=bool(deadline_date),
                deadline_date=deadline_date,
            )
            # ``completed_at`` is not a column on ``store.create``'s
            # signature (it's normally stamped by state-transition
            # logic). For lines that arrive already-done with a ✅
            # date, we backfill it via a post-create update below so
            # the drift loops only have to handle the existing-tasks
            # case.
            if initial_state == "done" and info.get("completed_at"):
                try:
                    store.update(
                        task_id,
                        completed_at=info["completed_at"],
                        reason="task_sync: completed_at backfilled at create from ✅ date",
                    )
                except Exception as exc:
                    logger.warning(
                        "task_sync: failed to backfill completed_at for %s: %s",
                        task_id, exc,
                    )
            created.append(task_id)
            logger.info(
                "task_sync: created store record for %s "
                "(state=%s, urgency=%s, line=%d, note_uuid=%s)",
                task_id, initial_state, initial_urgency,
                info["line_number"], info.get("note_uuid"),
            )
        except Exception as exc:
            logger.warning("task_sync: failed to create store for %s: %s", task_id, exc)

    # 2. In store but not in file → soft-delete from store.
    #    The file is the source of truth: if it's gone from the file,
    #    flag the store row as deleted. This is a safe operation —
    #    ``store.delete()`` sets ``deleted_at`` rather than removing
    #    the row, so recovery is a single ``store.restore(task_id)``
    #    away. Cascading FKs do not fire on this path since nothing is
    #    actually DROPPED.
    deleted_from_store: list[str] = []
    for task_id in store_ids - file_ids:
        record = store_by_id[task_id]
        try:
            if store.delete(task_id):
                deleted_from_store.append(task_id)
                logger.info(
                    "task_sync: soft-deleted orphan-in-store %s (was state=%s)",
                    task_id, record["state"],
                )
        except Exception as exc:
            logger.warning(
                "task_sync: failed to soft-delete orphan %s: %s",
                task_id, exc,
            )

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
                    "task_sync: resolved mismatch %s - store %s -> %s (line %d)",
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

    # 6. urgency drift (file emoji → store column).
    #
    #    The Obsidian Tasks plugin owns the markdown urgency emoji
    #    (⏫ / 🔼 / 🔽). The store's ``urgency`` column has existed
    #    forever but the parser used to ignore the emoji — every
    #    auto-created row landed with the hardcoded default ``medium``.
    #    Now we propagate the file value when present.
    #
    #    Same non-null discipline as ``note_uuid`` and ``description``:
    #    a missing emoji does NOT clear the store's value. A user can
    #    intentionally set urgency via ``task_update`` and not bother
    #    writing the emoji on the line — clearing on missing-emoji
    #    would silently undo that.
    resolved_urgencies: list[dict[str, Any]] = []
    for task_id in file_ids & store_ids:
        file_info = file_tasks[task_id]
        store_record = store_by_id[task_id]

        file_urgency = file_info.get("urgency")
        store_urgency = store_record.get("urgency")

        if file_urgency and file_urgency != store_urgency:
            try:
                store.update(
                    task_id,
                    urgency=file_urgency,
                    reason="task_sync: urgency drift from file",
                )
                resolved_urgencies.append({
                    "task_id": task_id,
                    "old_urgency": store_urgency,
                    "new_urgency": file_urgency,
                    "line_number": file_info["line_number"],
                })
                logger.info(
                    "task_sync: reconciled urgency %s — store %s -> %s (line %d)",
                    task_id, store_urgency, file_urgency,
                    file_info["line_number"],
                )
            except Exception as exc:
                logger.warning(
                    "task_sync: failed to reconcile urgency %s: %s", task_id, exc,
                )

    # 7. deadline_date drift (📅 in file → deadline_date column).
    #
    #    The store's ``has_deadline`` is a bool tied to whether
    #    ``deadline_date`` is set. We move them together: present in
    #    file → both filled in store; absent in file → no clear (same
    #    discipline as urgency above).
    resolved_deadlines: list[dict[str, Any]] = []
    for task_id in file_ids & store_ids:
        file_info = file_tasks[task_id]
        store_record = store_by_id[task_id]

        file_deadline = file_info.get("deadline_date")
        store_deadline = store_record.get("deadline_date")

        if file_deadline and file_deadline != store_deadline:
            try:
                store.update(
                    task_id,
                    has_deadline=True,
                    deadline_date=file_deadline,
                    reason="task_sync: deadline_date drift from file",
                )
                resolved_deadlines.append({
                    "task_id": task_id,
                    "old_deadline_date": store_deadline,
                    "new_deadline_date": file_deadline,
                    "line_number": file_info["line_number"],
                })
                logger.info(
                    "task_sync: reconciled deadline %s — store %s -> %s (line %d)",
                    task_id, store_deadline, file_deadline,
                    file_info["line_number"],
                )
            except Exception as exc:
                logger.warning(
                    "task_sync: failed to reconcile deadline %s: %s", task_id, exc,
                )

    # 8. completed_at drift (✅ <date> in file → completed_at column).
    #
    #    Only meaningful for done tasks — but we don't gate on state
    #    here because the checkbox-mismatch loop (#3) already wrote
    #    state='done' upstream this run if the file says so, and the
    #    state→done auto-stamp uses sync time. Backfilling the actual
    #    ✅ date corrects that to what the user wrote.
    resolved_completed_at: list[dict[str, Any]] = []
    for task_id in file_ids & store_ids:
        file_info = file_tasks[task_id]
        store_record = store_by_id[task_id]

        file_completed = file_info.get("completed_at")
        store_completed = store_record.get("completed_at")

        if file_completed and file_completed != store_completed:
            try:
                store.update(
                    task_id,
                    completed_at=file_completed,
                    reason="task_sync: completed_at drift from file ✅ date",
                )
                resolved_completed_at.append({
                    "task_id": task_id,
                    "old_completed_at": store_completed,
                    "new_completed_at": file_completed,
                    "line_number": file_info["line_number"],
                })
                logger.info(
                    "task_sync: reconciled completed_at %s — store %s -> %s (line %d)",
                    task_id, store_completed, file_completed,
                    file_info["line_number"],
                )
            except Exception as exc:
                logger.warning(
                    "task_sync: failed to reconcile completed_at %s: %s", task_id, exc,
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

    # --- Summary ---
    total_updates = (
        len(resolved_mismatches)
        + len(resolved_note_uuids)
        + len(resolved_descriptions)
        + len(resolved_urgencies)
        + len(resolved_deadlines)
        + len(resolved_completed_at)
    )
    total_actions = len(created) + len(deleted_from_store) + total_updates
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
        "resolved_urgencies": len(resolved_urgencies),
        "resolved_deadlines": len(resolved_deadlines),
        "resolved_completed_at": len(resolved_completed_at),
        "tag_rows_written": tag_rows_written,
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
    if resolved_urgencies:
        result["urgency_details"] = resolved_urgencies
    if resolved_deadlines:
        result["deadline_details"] = resolved_deadlines
    if resolved_completed_at:
        result["completed_at_details"] = resolved_completed_at

    if total_actions > 0:
        logger.info(
            "task_sync: %d actions — %d created, %d deleted, %d mismatches, "
            "%d note_uuids, %d descriptions, %d urgencies, %d deadlines, "
            "%d completed_at",
            total_actions, len(created), len(deleted_from_store),
            len(resolved_mismatches), len(resolved_note_uuids),
            len(resolved_descriptions), len(resolved_urgencies),
            len(resolved_deadlines), len(resolved_completed_at),
        )
    else:
        logger.debug("task_sync: all clean (%d file, %d store)", len(file_ids), len(store_ids))

    # Record this run's completion in the sync-status table so the
    # dashboard can render "synced Xm ago". Best-effort: a write
    # failure here must not undo the reconciliation that already
    # landed above.
    try:
        store.set_sync_status(
            created=len(created),
            updated=total_updates,
            deleted=len(deleted_from_store),
        )
    except Exception as exc:
        logger.warning("task_sync: failed to record sync_status: %s", exc)

    return result
