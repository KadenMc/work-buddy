"""Index-domain ops — drive the consolidated index's flag-aware build.

Referenced by the ``index_rebuild`` capability declaration. The sidecar's per-partition
``index-<partition>-refresh`` jobs call this on a schedule; it no-ops while ``index.enabled``
is false and runs an incremental build of the named partition when enabled. When a
``partition`` is given it also self-skips (read-only advisory-lock
probe) if a build for that partition is already running, so a recurring refresh never
re-enters an in-flight build. The sidecar runs in its own process and may import heavy libs,
so the build runs in-process here (sharing the broker).
"""

from __future__ import annotations

import json

from work_buddy.mcp_server.op_registry import register_op


def _index_rebuild_dispatch(partition: str | None = None, force: bool = False) -> str:
    """Incrementally (re)build the consolidated index — flag-gated.

    Returns ``{"skipped": ...}`` while ``index.enabled`` is false, or
    ``{"skipped": "build_in_progress"}`` when a build for ``partition`` already holds the
    advisory lock. When enabled + free, builds ``partition`` (e.g. ``"knowledge"``) into the
    separate ``db/index-consolidated``, or all partitions when omitted. ``force=True`` rebuilds
    from scratch; the default is incremental (content-hash diff — cheap when nothing changed).
    """
    from work_buddy.index.config import load_index_config

    cfg = load_index_config()
    if not cfg.enabled:
        return json.dumps(
            {"skipped": "index.enabled is false (flag-gated; off by default)"}
        )

    # Self-skip a partition whose build is already running. The builder takes a BLOCKING
    # advisory lock (``index/build.py``) that raises after ~30s if a live holder owns it, so a
    # recurring refresh re-firing mid-build would stall-then-error every tick (a multi-hour
    # vault build would error on every fire). Probe the SAME per-partition lock target
    # read-only and bail cheaply — mirrors ``vault_ops``'s ``is_locked`` pre-check.
    if partition:
        from work_buddy.utils import index_lock

        db = cfg.resolved_db_path()
        if index_lock.is_locked(db.parent / f"{db.name}.{partition}"):
            return json.dumps({"skipped": "build_in_progress", "partition": partition})

    from work_buddy.index.partitioned import UnifiedIndex

    ui = UnifiedIndex(config=cfg)
    result = ui.build(partition, force=force) if partition else ui.build_all(force=force)
    return json.dumps({"result": result}, default=str)


def _register() -> None:
    register_op("op.wb.index_rebuild", _index_rebuild_dispatch)


_register()
