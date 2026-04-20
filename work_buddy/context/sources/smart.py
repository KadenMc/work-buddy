"""``smart`` context source — Smart Connections (Obsidian) readiness + events.

The legacy collector degrades gracefully when Obsidian / Smart
Connections isn't available, so this wrapper is safe to register even
on machines without the plugin.
"""

from __future__ import annotations

from typing import Any

from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource
from work_buddy.context import registry as _registry


class SmartSource(MarkdownCollectorSource):
    name = "smart"
    _heading = "Smart Connections"
    _default_cfg: dict[str, Any] = {}

    def __init__(self):
        try:
            from work_buddy.collectors import smart_collector
            self._collect_fn = smart_collector.collect
        except Exception:
            self._collect_fn = None


_registry.register(SmartSource())
