"""Dispatch inline invocations from either the menu or tag surface.

The dispatcher is the single entry point that both surfaces funnel into:

1. resolve the command name from the payload,
2. build the :class:`InlineContext`,
3. either register a persistent watcher or execute the handler,
4. apply the declared consume mode, and
5. record the outcome in the invocation log.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

from work_buddy.inline import consume, context as context_mod, registry, store

logger = logging.getLogger(__name__)


async def dispatch(surface: str, payload: dict) -> dict:
    """Async entry point — see module docstring."""
    command_name = payload.get("command")
    if not command_name and surface == "tag":
        raw = payload.get("tag", "") or ""
        raw = raw.lstrip("#")
        if raw.startswith("wb/cmd/"):
            command_name = raw[len("wb/cmd/") :]
    if not command_name:
        return {"error": "no_command"}

    cmd = registry.get(command_name)
    if cmd is None:
        return {"error": "unknown_command", "command": command_name}

    if surface not in cmd.surfaces:
        return {"error": "surface_unsupported", "command": command_name, "surface": surface}

    try:
        ctx = context_mod.build_context(surface, payload, cmd.context_scope)
    except ValueError as exc:
        return {"error": str(exc)}

    # Persistent tag: register a watcher instead of executing immediately.
    if cmd.persistent and surface == "tag":
        tag_name = (ctx.tag or {}).get("name", "")
        tag_line = (ctx.tag or {}).get("line")
        watcher = store.create_watcher(
            command_name=command_name,
            file_path=ctx.file_path or "",
            tag=tag_name,
            tag_line=tag_line,
            params=payload.get("params") or {},
            schedule=payload.get("schedule"),
        )
        return {
            "registered": watcher.watcher_id,
            "command": command_name,
            "persistent": True,
        }

    inv_id = store.log_invocation(
        command_name=command_name,
        surface=surface,
        context=ctx.to_dict(),
    )

    if cmd.handler is None:
        store.update_invocation(inv_id, "failed", {"error": "no_handler"})
        return {"invocation_id": inv_id, "error": "no_handler"}

    try:
        if inspect.iscoroutinefunction(cmd.handler):
            result = await cmd.handler(ctx)
        else:
            result = cmd.handler(ctx)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Inline handler %s raised", command_name)
        err = {"error": str(exc), "type": type(exc).__name__}
        store.update_invocation(inv_id, "failed", err)
        return {"invocation_id": inv_id, "error": str(exc)}

    if not isinstance(result, dict):
        result = {"result": result}

    try:
        consume_result = consume.apply(cmd.consume_mode, ctx, result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Consume step failed for %s", command_name)
        consume_result = {"mutated": False, "note": f"consume_error:{exc}"}

    store.update_invocation(inv_id, "completed", result)
    return {
        "invocation_id": inv_id,
        "command": command_name,
        "result": result,
        "consume": consume_result,
    }


def dispatch_sync(surface: str, payload: dict) -> dict:
    """Synchronous wrapper used from the MCP registry.

    Falls back gracefully if called from inside an already-running loop
    (creates a fresh loop in a thread would be heavier; this is fine
    for sidecar-style callers).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(dispatch(surface, payload))
    # Running loop exists — run inline via a new event loop in a thread
    # so we don't deadlock. Keep it simple with asyncio.new_event_loop.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(dispatch(surface, payload))
    finally:
        loop.close()
