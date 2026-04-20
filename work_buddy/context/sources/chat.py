"""``chat`` context source — recent Claude / SpecStory conversation sessions."""

from __future__ import annotations

from typing import Any

from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource
from work_buddy.context import registry as _registry


class ChatSource(MarkdownCollectorSource):
    name = "chat"
    _heading = "Recent Chat Sessions"
    _default_cfg: dict[str, Any] = {}

    def __init__(self):
        try:
            from work_buddy.collectors import chat_collector
            self._collect_fn = chat_collector.collect
        except Exception:
            self._collect_fn = None


_registry.register(ChatSource())
