"""Shared advisory-lock boundary for consolidated-index writers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from work_buddy.logging_config import get_logger


logger = get_logger(__name__)


@contextmanager
def index_writer_locks(
    db_path: str | Path,
    partition: str,
    *,
    enabled: bool = True,
) -> Iterator[None]:
    """Hold the DB-wide writer gate, then one partition's identity lock.

    All consolidated-index mutations use this fixed order.  ``enabled=False``
    remains available for isolated unit builds; production maintenance operators
    use the default and therefore share the exact build exclusion boundary.
    """

    if not enabled:
        yield
        return
    try:
        from work_buddy.utils.index_lock import index_lock
    except Exception as exc:  # lock infra unavailable -> proceed best-effort
        logger.debug("index_lock unavailable (%s); writing without a lock", exc)
        yield
        return

    db = Path(db_path)
    gate = db.parent / f"{db.name}.build"
    identity = db.parent / f"{db.name}.{partition}"
    with index_lock(gate):
        with index_lock(identity):
            yield


__all__ = ["index_writer_locks"]
