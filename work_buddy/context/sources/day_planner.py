"""``day_planner`` context source — today's Day Planner section from Obsidian."""

from __future__ import annotations

from typing import Any

from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource
from work_buddy.context import registry as _registry


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


_registry.register(DayPlannerSource())
