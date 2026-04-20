"""``datacore`` context source — Datacore plugin query results."""

from __future__ import annotations

from typing import Any

from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource
from work_buddy.context import registry as _registry


class DatacoreSource(MarkdownCollectorSource):
    name = "datacore"
    _heading = "Datacore"
    _default_cfg: dict[str, Any] = {}

    def __init__(self):
        try:
            from work_buddy.collectors import datacore_collector
            self._collect_fn = datacore_collector.collect
        except Exception:
            self._collect_fn = None


_registry.register(DatacoreSource())
