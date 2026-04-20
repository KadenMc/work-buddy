"""``session_activity`` context source — current-session MCP activity ledger."""

from __future__ import annotations

from typing import Any

from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource
from work_buddy.context import registry as _registry


class SessionActivitySource(MarkdownCollectorSource):
    name = "session_activity"
    _heading = "Session Activity"
    _default_cfg: dict[str, Any] = {}

    def __init__(self):
        try:
            from work_buddy.collectors import session_activity_collector
            self._collect_fn = session_activity_collector.collect
        except Exception:
            self._collect_fn = None


_registry.register(SessionActivitySource())
