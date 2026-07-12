"""``agent_session_summary`` context source — interpreted recent activity.

Sibling to ``chat`` (raw inventory of recent agent-harness conversations) and
``session_activity`` (the *current* MCP session's activity ledger). This source
is the **interpreted** layer: it pulls observed agent sessions (Claude Code,
Codex, …) from the conversation_observability DB, joins their commits, dirty
file-writes, and PR activity, and renders one compact markdown block per
project with each session's tldr + topic timeline.

Output file in a context bundle: ``agent_session_summary.md``.
"""

from __future__ import annotations

from typing import Any

from work_buddy.context import registry as _registry
from work_buddy.context.sources._markdown_wrapper import MarkdownCollectorSource


class AgentSessionSummarySource(MarkdownCollectorSource):
    name = "agent_session_summary"
    _heading = "Agent Session Summary"
    _default_cfg: dict[str, Any] = {
        "days": 7,
        "refresh": True,
        "include_tldr": True,
        "include_topics": True,
    }

    def __init__(self):
        try:
            from work_buddy.collectors import agent_session_summary_collector

            self._collect_fn = agent_session_summary_collector.collect
        except Exception:
            self._collect_fn = None


_registry.register(AgentSessionSummarySource())
