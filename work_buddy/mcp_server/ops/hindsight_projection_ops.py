"""Bounded maintenance entry point for Truth-derived Hindsight memory."""

from __future__ import annotations

from typing import Any

from work_buddy.mcp_server.op_registry import register_op


def truth_hindsight_projection_tick(
    store_id: str | None = None,
    limit_per_store: int = 20,
    reconcile: bool = True,
) -> dict[str, Any]:
    """Reconcile Truth desired state, then drain a bounded outbox batch."""

    from work_buddy.hindsight_projection.runtime import run_projection_tick

    return run_projection_tick(
        store_id=store_id,
        limit_per_store=limit_per_store,
        reconcile=reconcile,
    )


register_op(
    "op.wb.truth_hindsight_projection_tick",
    truth_hindsight_projection_tick,
    replace=True,
)


__all__ = ["truth_hindsight_projection_tick"]
