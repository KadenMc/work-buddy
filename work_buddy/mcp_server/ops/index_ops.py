"""Index-domain ops — drive the consolidated index's flag-aware build.

Referenced by the ``index_rebuild`` capability declaration. The sidecar's
``consolidated-index-rebuild`` job calls this on a schedule; it no-ops while
``index.enabled`` is false (so it ships inert) and runs an incremental build of the
consolidated index once the flag is flipped. The sidecar runs in its own process and
may import heavy libs, so the build runs in-process here (sharing the broker).
"""

from __future__ import annotations

import json

from work_buddy.mcp_server.op_registry import register_op


def _index_rebuild_dispatch(partition: str | None = None, force: bool = False) -> str:
    """Incrementally (re)build the consolidated index — flag-gated.

    Returns ``{"skipped": ...}`` while ``index.enabled`` is false. When enabled, builds
    ``partition`` (e.g. ``"knowledge"``) into the separate ``db/index-consolidated``, or
    all partitions when omitted. ``force=True`` rebuilds from scratch; the default is
    incremental (content-hash diff — cheap when nothing changed).
    """
    from work_buddy.index.config import load_index_config

    cfg = load_index_config()
    if not cfg.enabled:
        return json.dumps(
            {"skipped": "index.enabled is false (flag-gated; off by default)"}
        )
    from work_buddy.index.partitioned import UnifiedIndex

    ui = UnifiedIndex(config=cfg)
    result = ui.build(partition, force=force) if partition else ui.build_all(force=force)
    return json.dumps({"result": result}, default=str)


def _register() -> None:
    register_op("op.wb.index_rebuild", _index_rebuild_dispatch)


_register()
