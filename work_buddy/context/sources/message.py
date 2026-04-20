"""``message`` context source — inter-agent messaging inbox state."""

from __future__ import annotations

from typing import Any

from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource
from work_buddy.context import registry as _registry


class MessageSource(MarkdownCollectorSource):
    name = "message"
    _heading = "Messages"
    _default_cfg: dict[str, Any] = {}

    def __init__(self):
        try:
            from work_buddy.collectors import message_collector
            self._collect_fn = message_collector.collect
        except Exception:
            self._collect_fn = None


_registry.register(MessageSource())
