"""``claude_session_summary`` context source — interpreted recent activity.

Sibling to ``chat`` (raw inventory of recent Claude Code chats) and
``session_activity`` (the *current* MCP session's activity ledger).
This source is the **interpreted** layer: it pulls observed Claude
Code sessions from the conversation_observability DB, joins their
commits and dirty file-writes, and renders one compact markdown block
per project.

Output file in a context bundle: ``claude_session_summary.md``.
"""

from __future__ import annotations

from typing import Any

from work_buddy.context import registry as _registry
from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource


class ClaudeSessionSummarySource(MarkdownCollectorSource):
    name = "claude_session_summary"
    _heading = "Claude Session Summary"
    _default_cfg: dict[str, Any] = {"days": 7, "refresh": True}

    def __init__(self):
        try:
            from work_buddy.collectors import claude_session_summary_collector

            self._collect_fn = claude_session_summary_collector.collect
        except Exception:
            self._collect_fn = None


_registry.register(ClaudeSessionSummarySource())
