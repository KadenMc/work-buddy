"""Dashboard glue for the Claude-Code-usage cost source.

This thin module exists so the ``/api/costs`` and ``/api/costs/rescan``
routes in :mod:`work_buddy.dashboard.service` can lazy-import a stable
public surface without depending on the vendored scanner internals.
"""

from __future__ import annotations

from typing import Any


def get_claude_code_usage_summary(*, project: str | None = None) -> dict[str, Any]:
    """Read the Claude-Code-usage cache into the Costs-tab read model."""
    from work_buddy.llm.claude_code_usage.aggregator import (
        get_claude_code_usage_summary as _impl,
    )
    return _impl(project=project)


def rescan_claude_code_usage(*, full_rebuild: bool = False) -> dict[str, Any]:
    """Re-scan Claude Code transcripts into the cache. Returns ingestion stats."""
    from work_buddy.llm.claude_code_usage.scanner import scan
    return scan(full_rebuild=full_rebuild)
