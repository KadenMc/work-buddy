"""Phase-2 dashboard glue for the transcript-derived cost source.

This thin module exists so the ``/api/costs`` and ``/api/costs/rescan``
routes in :mod:`work_buddy.dashboard.service` can lazy-import a stable
public surface without depending on the vendored scanner internals.
"""

from __future__ import annotations

from typing import Any


def get_transcripts_summary() -> dict[str, Any]:
    """Read the transcripts cache into the Costs-tab read model."""
    from work_buddy.llm.transcripts.aggregator import (
        get_transcripts_summary as _impl,
    )
    return _impl()


def rescan_transcripts(*, full_rebuild: bool = False) -> dict[str, Any]:
    """Re-scan Claude Code transcripts. Returns ingestion stats."""
    from work_buddy.llm.transcripts.scanner import scan
    return scan(full_rebuild=full_rebuild)
