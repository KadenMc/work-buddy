"""Reconcile vault ``#wb/cmd/*`` tags with the persistent watcher store.

Same canonical-source-of-truth discipline as task-sync: the vault is
authoritative. Tags present in the vault but not in the store become
new watchers; watchers whose tag has disappeared get cancelled.
"""

from __future__ import annotations

import logging

from work_buddy.inline import registry, store

logger = logging.getLogger(__name__)


def _vault_tags_for_command(command_name: str) -> list[dict]:
    """Return list of ``{file_path, tag, tag_line}`` for the command's tag.

    Tries ``search_by_tag(..., mode="prefix")`` first; falls back to
    ``get_all_tags(include_files=True)`` if the helper shape doesn't match.
    """
    wanted = f"wb/cmd/{command_name}"
    try:
        from work_buddy.obsidian.tags import search_by_tag

        result = search_by_tag(wanted, mode="prefix")
        out: list[dict] = []
        for entry in (result or {}).get("files", []):
            path = entry.get("path") if isinstance(entry, dict) else entry
            if not path:
                continue
            out.append({"file_path": path, "tag": wanted, "tag_line": None})
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("search_by_tag failed for %s: %s", wanted, exc)

    try:
        from work_buddy.obsidian.tags import get_all_tags

        rows = get_all_tags(include_files=True)
        out = []
        for row in rows or []:
            tag = (row.get("tag") or "").lstrip("#")
            if tag == wanted or tag.startswith(wanted + "/"):
                for fp in row.get("files", []) or []:
                    out.append({"file_path": fp, "tag": tag, "tag_line": None})
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_all_tags fallback failed: %s", exc)
        return []


def inline_sync() -> dict:
    """Reconcile vault tags with watcher store for every persistent command."""
    added: list[dict] = []
    removed: list[dict] = []
    due: list[dict] = []

    for cmd in registry.list_commands():
        if not cmd.persistent:
            continue

        vault_hits = _vault_tags_for_command(cmd.name)
        vault_key = {(h["file_path"], h["tag"]) for h in vault_hits}

        existing = store.list_watchers(command_name=cmd.name)
        existing_key = {(w.file_path, w.tag): w for w in existing}

        # Added: vault - store
        for hit in vault_hits:
            k = (hit["file_path"], hit["tag"])
            if k not in existing_key:
                w = store.create_watcher(
                    command_name=cmd.name,
                    file_path=hit["file_path"],
                    tag=hit["tag"],
                    tag_line=hit.get("tag_line"),
                )
                added.append(w.to_dict())

        # Removed: store - vault
        for (fp, tg), w in existing_key.items():
            if (fp, tg) not in vault_key:
                if store.delete_watcher(w.watcher_id):
                    removed.append(w.to_dict())

        # Due watchers — TODO: real cron evaluation; for now just collect
        # watchers with a schedule that have never run. Follow-up work:
        # enqueue through work_buddy/sidecar/retry_sweep.py.
        for w in store.list_watchers(command_name=cmd.name):
            if w.schedule and not w.last_run_at:
                due.append(w.to_dict())

    logger.info(
        "inline_sync: added=%d removed=%d due=%d", len(added), len(removed), len(due)
    )
    return {"added": added, "removed": removed, "due_to_run": due}
