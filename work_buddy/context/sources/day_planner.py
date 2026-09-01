"""``day_planner`` context source — today's Day Planner section from Obsidian."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource
from work_buddy.context import registry as _registry
from work_buddy.context.types import ContextRequest, ContextSection


class DayPlannerSource(MarkdownCollectorSource):
    name = "day_planner"
    _heading = "Today's Day Planner"
    _default_cfg: dict[str, Any] = {}

    def __init__(self):
        try:
            from work_buddy.collectors import day_planner_collector
            self._collect_fn = day_planner_collector.collect
        except Exception:
            self._collect_fn = None

    def is_stale(self, cached: ContextSection, request: ContextRequest) -> bool:
        from work_buddy.collectors.obsidian_collector import (
            _native_journal_authority,
        )

        return _native_journal_authority(self._build_cfg(request))

    def serves_native_without_obsidian(self, request: ContextRequest) -> bool:
        from work_buddy.collectors.obsidian_collector import (
            _native_journal_authority,
        )

        return _native_journal_authority(self._build_cfg(request))

    def collection_guard(self, request: ContextRequest):
        from work_buddy.collectors.obsidian_collector import (
            _native_journal_authority,
        )

        if _native_journal_authority(self._build_cfg(request)):
            return nullcontext()
        from work_buddy.journal_capture.authority import legacy_markdown_write_guard

        return legacy_markdown_write_guard()


_registry.register(DayPlannerSource())
