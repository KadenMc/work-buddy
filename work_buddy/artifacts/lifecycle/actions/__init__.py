"""ExpiryAction implementations — what happens to expired records.

Two actions, justified by the one-consumer rule:

* :class:`Delete` — the default. Used by every consumer that just
  wants expired records gone.
* :class:`TransformAndDelete` — used by claude_code_usage to roll up
  per-turn rows into daily aggregates before deleting the originals.

Speculative actions (Archive, Compact, Snapshot, …) are deliberately
NOT built — added when a real consumer needs them.
"""

from __future__ import annotations

from work_buddy.artifacts.lifecycle.actions.delete import Delete
from work_buddy.artifacts.lifecycle.actions.transform_and_delete import (
    TransformAndDelete,
)

__all__ = ["Delete", "TransformAndDelete"]
