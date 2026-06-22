"""Events-backbone ops (agent-facing capabilities).

Each op here is referenced by a ``kind: capability`` knowledge-store unit
carrying a matching ``op`` field (``knowledge/store/events/``).
"""

from __future__ import annotations

from typing import Any

from work_buddy.mcp_server.op_registry import register_op


def event_publish(
    type: str,  # noqa: A002 — mirrors the CloudEvents attribute name
    data: dict[str, Any] | None = None,
    source: str = "/wb/agent",
    durable: bool = True,
    subject: str | None = None,
) -> dict[str, Any]:
    """Publish one event onto the Events backbone (fire-and-forget).

    Durable events are logged (deduped on ``(source, id)``) and delivered
    at-least-once to registered consumers; ``durable=False`` is a lossy
    UI-only fan-out that never hits the log.
    """
    from work_buddy.events.dispatcher import publish
    from work_buddy.events.envelope import new_event

    evt = new_event(
        source, type, data or {}, durable=durable, subject=subject, modality="internal"
    )
    publish(evt)
    return {
        "ok": True,
        "id": evt.id,
        "type": evt.type,
        "source": evt.source,
        "durable": durable,
    }


def event_sources_poll() -> dict[str, Any]:
    """Poll every enabled, due event source once — the cron-fired tick.

    For each source whose ``interval`` has elapsed: fetch → diff the watched value
    vs. the stored cursor → on a meaningful change, publish
    ``ai.workbuddy.source.<name>.changed`` onto the spine (the ``source-action``
    consumer reacts). Runs **in-process in the sidecar**, so it fetches and
    publishes directly — no gateway round-trip.
    """
    from work_buddy.events.sources.poller import poll_due_sources

    return poll_due_sources()


_DRY_RUN_BUILD_KEYS = frozenset({
    "source_type", "interval", "url", "extract_mode", "extract_path", "condition",
    "action", "action_params", "allowed_actions", "autonomy", "max_per_hour",
    "cursor_from", "enabled", "event_type", "semantic",
})


def event_source_dry_run(
    name: str | None = None,
    proposal: dict[str, Any] | None = None,
    run_semantic: bool = False,
) -> dict[str, Any]:
    """Preview a source without side effects: fetch → diff → evaluate condition,
    but **never** publish, run an action, or advance the cursor. Returns the
    sampled value, whether it changed, the would-emit event, and whether the
    condition would pass. This is what the ``/wb-event-new`` dry-run step shows.

    Pass ``proposal`` (the structured ``event_source_create`` fields) to preview a
    not-yet-written source; otherwise pass ``name`` to preview a saved one.
    """
    from work_buddy.events.conditions.cel import CelCondition
    from work_buddy.events.envelope import new_event
    from work_buddy.events.protocol import ConditionContext
    from work_buddy.events.sources import poller
    from work_buddy.events.sources.definition import (
        build_source_fm,
        from_frontmatter,
        validate_source_fm,
    )
    from work_buddy.events.sources.loader import load_event_sources

    if proposal is not None:
        pname = proposal.get("name") or name or "preview"
        build_kwargs = {k: v for k, v in proposal.items() if k in _DRY_RUN_BUILD_KEYS}
        try:
            fm = build_source_fm(**build_kwargs)
        except TypeError as exc:  # missing required field (source_type / interval)
            return {"ok": False, "error": f"proposal is missing required fields: {exc}"}
        errs = validate_source_fm(pname, fm)
        if errs:
            return {"ok": False, "error": "; ".join(errs), "errors": errs}
        src = from_frontmatter(pname, fm)
    else:
        defs, errors = load_event_sources()
        src = next((d for d in defs if d.name == name), None)
        if src is None:
            return {"ok": False, "error": f"no event source named {name!r}", "errors": errors}

    result = poller.dry_run(src)
    would_emit = result.get("would_emit")
    out: dict[str, Any] = {
        "ok": True,
        "source": name,
        "type": src.type,
        "changed": result.get("changed"),
        "is_first": result.get("is_first"),
        "current": result.get("value"),
        "prev": result.get("prev"),
        "would_emit": would_emit,
        "error": result.get("error"),
    }

    evt = None
    if would_emit is not None:
        evt = new_event(
            src.source_uri,
            src.event_type,
            data={
                "current": result.get("value"),
                "prev": result.get("prev"),
                "source_name": src.name,
            },
            modality="pull",
        )

    cond_passed = True
    if src.condition and evt is not None:
        try:
            cond_passed = CelCondition(src.condition).evaluate(evt, None, ConditionContext())
        except Exception as exc:  # noqa: BLE001
            cond_passed = False
            out["condition_error"] = str(exc)
        out["condition_passed"] = cond_passed

    out["would_fire"] = bool(evt is not None and cond_passed)

    # Tier-3 semantic gate: reported always; only *evaluated* when the caller
    # opts in (it makes a real search + local-LLM call). The evaluation uses an
    # ephemeral state dir so a preview never pollutes real cooldown/cursor state.
    if src.semantic:
        out["semantic_configured"] = True
        out["semantic_question"] = src.semantic.get("question")
        if run_semantic and out["would_fire"] and evt is not None:
            import tempfile
            from pathlib import Path

            from work_buddy.events.conditions.semantic_llm import SemanticLlmCondition

            try:
                with tempfile.TemporaryDirectory() as td:
                    sem = SemanticLlmCondition(src, state_directory=Path(td)).evaluate(
                        evt, None, ConditionContext()
                    )
            except Exception as exc:  # noqa: BLE001
                sem = False
                out["semantic_error"] = str(exc)
            out["semantic_passed"] = bool(sem)
            out["would_fire"] = bool(out["would_fire"] and sem)
        else:
            out["semantic_pending"] = True
    return out


def event_source_create(
    name: str,
    source_type: str,
    interval: str,
    url: str | None = None,
    extract_mode: str = "hash",
    extract_path: str | None = None,
    condition: str | None = None,
    action: str = "notify",
    action_params: dict[str, Any] | None = None,
    allowed_actions: list[str] | None = None,
    autonomy: str = "notify_only",
    max_per_hour: int | None = None,
    cursor_from: str = "now",
    enabled: bool = True,
    event_type: str | None = None,
    semantic: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Author an event source: build + validate its frontmatter, then write
    ``<event_sources>/<name>.md``. Refuses to overwrite unless ``overwrite=True``;
    a malformed source returns ``{"success": false, "errors": [...]}``."""
    from work_buddy.events.sources.definition import build_source_fm
    from work_buddy.events.sources.loader import sources_dir, write_event_source

    fm = build_source_fm(
        source_type=source_type,
        interval=interval,
        url=url,
        extract_mode=extract_mode,
        extract_path=extract_path,
        condition=condition,
        action=action,
        action_params=action_params,
        allowed_actions=allowed_actions,
        autonomy=autonomy,
        max_per_hour=max_per_hour,
        cursor_from=cursor_from,
        enabled=enabled,
        event_type=event_type,
        semantic=semantic,
    )
    return write_event_source(sources_dir(), name, fm, overwrite=overwrite)


def event_source_list() -> dict[str, Any]:
    """List authored event sources (valid + invalid). Read-only."""
    from work_buddy.events.sources.loader import load_event_sources

    defs, errors = load_event_sources()
    return {
        "ok": True,
        "sources": [
            {
                "name": d.name,
                "type": d.type,
                "interval_s": d.interval_s,
                "enabled": d.enabled,
                "action": d.action_name,
                "allowed_actions": list(d.allowed_actions),
                "condition": d.condition,
                "semantic": bool(d.semantic),
                "autonomy": d.autonomy,
            }
            for d in defs
        ],
        "errors": errors,
    }


def event_source_toggle(name: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable an authored event source (rewrites its ``.md``)."""
    from work_buddy.events.sources.loader import (
        load_event_sources,
        sources_dir,
        write_event_source,
    )

    defs, errors = load_event_sources()
    src = next((d for d in defs if d.name == name), None)
    if src is None:
        return {"ok": False, "error": f"no event source named {name!r}", "errors": errors}

    fm = dict(src.raw)
    fm["enabled"] = bool(enabled)
    res = write_event_source(sources_dir(), name, fm, overwrite=True)
    return {"ok": bool(res.get("success")), "enabled": bool(enabled), **res}


def _register() -> None:
    register_op("op.wb.event_publish", event_publish)
    register_op("op.wb.event_sources_poll", event_sources_poll)
    register_op("op.wb.event_source_dry_run", event_source_dry_run)
    register_op("op.wb.event_source_create", event_source_create)
    register_op("op.wb.event_source_list", event_source_list)
    register_op("op.wb.event_source_toggle", event_source_toggle)


_register()
