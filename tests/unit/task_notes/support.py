from __future__ import annotations

from typing import Any

from work_buddy.journal_capture.migration import CALLSITE_INVENTORY_SHA256


def current_journal_exit_evidence() -> dict[str, Any]:
    """Synthetic current receipt for isolated task-note state-machine tests."""

    return {
        "receipt_id": "a" * 32,
        "inventory_sha256": "b" * 64,
        "callsite_inventory_sha256": CALLSITE_INVENTORY_SHA256,
        "authority_summary": {
            "schema": "wb.journal-exit-evidence/v1",
            "days": 1,
            "entities": 1,
            "cutoverGate": "open",
        },
        "created_at": "2026-08-10T00:00:00+00:00",
    }


__all__ = ["current_journal_exit_evidence"]
