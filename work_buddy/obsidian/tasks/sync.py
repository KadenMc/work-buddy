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

from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.obsidian.tasks import store
from work_buddy.obsidian.tasks.mutations import TASK_ID_RE, MASTER_TASK_FILE

logger = get_logger(__name__)


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

        tasks[task_id] = {
            "line_number": i + 1,
            "is_done": is_done,
            "line": line_stripped,
        }

    return tasks


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
            store.create(task_id=task_id, state=initial_state, urgency="medium")
            created.append(task_id)
            logger.info(
                "task_sync: created store record for %s (state=%s, line=%d)",
                task_id, initial_state, info["line_number"],
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

    # --- Summary ---
    total_actions = len(created) + len(deleted_from_store) + len(resolved_mismatches)
    status = "ok" if total_actions == 0 else "synced"

    result: dict[str, Any] = {
        "status": status,
        "file_tasks": len(file_ids),
        "store_records": len(store_ids),
        "created": len(created),
        "deleted": len(deleted_from_store),
        "resolved_mismatches": len(resolved_mismatches),
    }

    # Include details only if actions were taken (keeps log concise)
    if created:
        result["created_details"] = created
    if deleted_from_store:
        result["deleted_details"] = deleted_from_store
    if resolved_mismatches:
        result["mismatch_details"] = resolved_mismatches

    if total_actions > 0:
        logger.info(
            "task_sync: %d actions — %d created, %d deleted, %d mismatches resolved",
            total_actions, len(created), len(deleted_from_store), len(resolved_mismatches),
        )
    else:
        logger.debug("task_sync: all clean (%d file, %d store)", len(file_ids), len(store_ids))

    return result
