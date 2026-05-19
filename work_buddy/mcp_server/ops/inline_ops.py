"""Inline Obsidian-command ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). These callables expose
the :mod:`work_buddy.inline` dispatcher, watcher store, and sync reconciler.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def inline_invoke(command: str, surface: str, payload: dict | None = None) -> dict:
    """Execute an inline command (menu or #wb/cmd/* tag surface)."""
    from work_buddy.inline import dispatcher

    merged = dict(payload or {})
    merged["command"] = command
    return dispatcher.dispatch_sync(surface, merged)


def inline_list_commands(surface: str | None = None) -> dict:
    """List registered inline commands, optionally filtered by surface."""
    from work_buddy.inline import registry as ireg

    cmds = ireg.list_for_surface(surface) if surface else ireg.list_commands()
    return {"commands": [c.to_dict() for c in cmds]}


def inline_menu_manifest() -> dict:
    """Manifest of inline commands that expose a right-click menu entry."""
    from work_buddy.inline import registry as ireg

    items = [
        {
            "command": c.name,
            "label": c.menu_label or c.name,
            "description": c.description,
        }
        for c in ireg.list_for_surface("menu")
    ]
    return {"items": items}


def inline_tag_removed(file_path: str, tag: str) -> dict:
    """Cancel persistent watchers whose tag was removed from a note."""
    from work_buddy.inline import store as istore

    cleaned = tag.lstrip("#")
    removed = []
    for w in istore.list_watchers(file_path=file_path):
        if w.tag == cleaned or w.tag == tag:
            if istore.delete_watcher(w.watcher_id):
                removed.append(w.watcher_id)
    return {"removed": removed, "count": len(removed)}


def inline_list_watchers() -> dict:
    """List all persistent inline watchers."""
    from work_buddy.inline import store as istore

    return {"watchers": [w.to_dict() for w in istore.list_watchers()]}


def inline_cancel_watcher(watcher_id: str) -> dict:
    """Cancel a single persistent watcher by ID."""
    from work_buddy.inline import store as istore

    ok = istore.delete_watcher(watcher_id)
    return {"cancelled": ok, "watcher_id": watcher_id}


def inline_sync() -> dict:
    """Reconcile vault #wb/cmd/* tags with the persistent watcher store."""
    from work_buddy.inline import sync as isync

    return isync.inline_sync()


def _register() -> None:
    register_op("op.wb.inline_invoke", inline_invoke)
    register_op("op.wb.inline_list_commands", inline_list_commands)
    register_op("op.wb.inline_menu_manifest", inline_menu_manifest)
    register_op("op.wb.inline_tag_removed", inline_tag_removed)
    register_op("op.wb.inline_list_watchers", inline_list_watchers)
    register_op("op.wb.inline_cancel_watcher", inline_cancel_watcher)
    register_op("op.wb.inline_sync", inline_sync)


_register()
