"""Index-domain ops — drive the consolidated index's flag-aware build.

Referenced by the ``index_rebuild`` capability declaration. The sidecar's per-partition
``index-<partition>-refresh`` jobs call this on a schedule; it no-ops while ``index.enabled``
is false and runs an incremental build of the named partition when enabled. It also
self-skips (read-only advisory-lock probe) while ANY index build is running — the
partitions share one single-writer SQLite DB, so builds serialize on a DB-wide writer
gate and a recurring refresh never piles onto an in-flight build of any partition.
The sidecar runs in its own process and may import heavy libs, so the build runs
in-process here (sharing the broker).
"""

from __future__ import annotations

import json

from work_buddy.mcp_server.op_registry import register_op


def _index_rebuild_dispatch(partition: str | None = None, force: bool = False) -> str:
    """Incrementally (re)build the consolidated index — flag-gated.

    Returns ``{"skipped": ...}`` while ``index.enabled`` is false, or
    ``{"skipped": "build_in_progress"}`` while any index build holds the DB-wide writer
    gate (or this partition's own lock). When enabled + free, builds ``partition``
    (e.g. ``"knowledge"``) into the
    separate ``db/index-consolidated``, or all partitions when omitted. ``force=True`` rebuilds
    from scratch; the default is incremental (content-hash diff — cheap when nothing changed).
    """
    from work_buddy.index.config import load_index_config

    cfg = load_index_config()
    if not cfg.enabled:
        return json.dumps(
            {"skipped": "index.enabled is false (flag-gated; off by default)"}
        )

    # Self-skip while ANY index build is running. All partitions share one SQLite DB
    # (single writer), so builds serialize on the DB-wide ``.build`` writer gate
    # (``index/build.py``) — and a refresh firing into a held gate would block ~30s on
    # the advisory acquire then error, every tick, for as long as the build runs (a
    # first vault/conversation build is multi-hour). Probe the gate (and, for the
    # message's sake, this partition's own lock) read-only and bail cheaply — mirrors
    # ``vault_ops``'s ``is_locked`` pre-check.
    from work_buddy.utils import index_lock

    db = cfg.resolved_db_path()
    targets = [db.parent / f"{db.name}.build"]
    if partition:
        targets.append(db.parent / f"{db.name}.{partition}")
    for target in targets:
        if index_lock.is_locked(target):
            return json.dumps({"skipped": "build_in_progress", "partition": partition})

    from work_buddy.index.partitioned import UnifiedIndex

    ui = UnifiedIndex(config=cfg)
    result = ui.build(partition, force=force) if partition else ui.build_all(force=force)
    return json.dumps({"result": result}, default=str)


def _register() -> None:
    register_op("op.wb.index_rebuild", _index_rebuild_dispatch)


_register()
