"""TransformAndDelete — rollup-style action for the claude_code_usage backend.

Wraps a caller-supplied ``transform_fn(conn, dry_run) -> dict`` so the
existing ``rollup_old_turns`` function can plug in unchanged. The
action calls the storage's ``open_connection()`` (defined on
SqliteRollupStorage) so the transform + delete happen in one
transaction.

Capabilities declared: TRANSFORM_ON_EXPIRY.

The ``Lifecycle.find_expired`` step is skipped for this action — the
trigger still informs the action of what would be expired (for
visibility), but the actual rollup work happens inside ``transform_fn``
which has the full SQL freedom to query, aggregate, and DELETE in one
transaction. ``expired_refs`` is passed for context but the action
doesn't iterate over it.
"""

from __future__ import annotations

from typing import Any, Callable

from work_buddy.artifacts.protocol import Capability, Ref, Storage


class TransformAndDelete:
    """Run a caller-supplied rollup function, then delete the originals.

    Args:
        transform_fn: Callable with signature
            ``(conn: sqlite3.Connection, *, dry_run: bool) -> dict``.
            The dict result should include ``rolled_turns`` (count) and
            ``rollup_groups`` (count). Existing
            :func:`work_buddy.llm.claude_code_usage.rollup.rollup_old_turns`
            already has this shape.
    """

    capabilities: frozenset[Capability] = frozenset({Capability.TRANSFORM_ON_EXPIRY})

    def __init__(
        self,
        *,
        transform_fn: Callable[..., dict[str, Any]],
    ) -> None:
        self._transform_fn = transform_fn

    def apply(
        self,
        storage: Storage,
        expired_refs: list[Ref],
        *,
        dry_run: bool,
    ) -> dict[str, int]:
        # The storage MUST be SqliteRollupStorage for this action to
        # work — that's enforced by the Artifact.__post_init__ coherence
        # check (TRANSFORM_ON_EXPIRY requires RECORDS storage). The
        # specific need for ``open_connection()`` is documented as the
        # contract between TransformAndDelete and SqliteRollupStorage.
        open_conn = getattr(storage, "open_connection", None)
        if open_conn is None:
            raise TypeError(
                f"TransformAndDelete requires a storage with open_connection(); "
                f"got {type(storage).__name__}"
            )
        conn = open_conn()
        try:
            result = self._transform_fn(conn, dry_run=dry_run)
        finally:
            conn.close()

        # Map the transform's result keys onto the action contract.
        return {
            "pruned": result.get("rolled_turns", result.get("pruned", 0)),
            "transformed": result.get("rollup_groups", result.get("transformed", 0)),
            "bytes_freed": 0,  # measured at the Artifact level via size_bytes
            **{k: v for k, v in result.items()
               if k not in ("rolled_turns", "rollup_groups", "pruned", "transformed")},
        }
