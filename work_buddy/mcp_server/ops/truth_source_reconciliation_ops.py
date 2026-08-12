"""Bounded recovery capability for committed Truth source usages."""

from __future__ import annotations

from typing import Any

from work_buddy.mcp_server.op_registry import register_op


def truth_source_usage_reconcile(
    store_id: str | None = None, limit_per_store: int = 100
) -> dict[str, Any]:
    from work_buddy.truth.source_reconciliation import reconcile_truth_source_usages

    return reconcile_truth_source_usages(
        store_id=store_id,
        limit_per_store=limit_per_store,
    )


register_op(
    "op.wb.truth_source_usage_reconcile",
    truth_source_usage_reconcile,
    replace=True,
)


__all__ = ["truth_source_usage_reconcile"]
