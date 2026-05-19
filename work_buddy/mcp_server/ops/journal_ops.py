"""Journal-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The closure code below
is moved verbatim from the former ``registry.py`` builder.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op



def _register() -> None:
    from work_buddy import journal
    from work_buddy.journal_backlog import read_running_notes
    from work_buddy.mcp_server.context_wrappers import (
        activity_timeline,
        day_planner,
        hot_files,
        journal_sign_in,
        journal_write,
    )

    register_op("op.wb.journal_state", journal.read_journal_state)
    register_op("op.wb.activity_timeline", activity_timeline)
    register_op("op.wb.hot_files", hot_files)
    register_op("op.wb.running_notes", read_running_notes)
    register_op("op.wb.journal_sign_in", journal_sign_in)
    register_op("op.wb.journal_write", journal_write)
    register_op("op.wb.day_planner", day_planner)
    register_op("op.wb.vault_write_at_location", lambda **kw: __import__('work_buddy.obsidian.vault_writer', fromlist=['write_at_location']).write_at_location(**kw))
    register_op("op.wb.obsidian_retry", lambda **kw: __import__('work_buddy.obsidian.retry', fromlist=['obsidian_retry']).obsidian_retry(**kw))
    register_op("op.wb.journal_route_to_tasks", lambda **kw: __import__('work_buddy.journal_backlog.thread_actions', fromlist=['journal_route_to_tasks']).journal_route_to_tasks(**kw))
    register_op("op.wb.journal_route_to_considerations", lambda **kw: __import__('work_buddy.journal_backlog.thread_actions', fromlist=['journal_route_to_considerations']).journal_route_to_considerations(**kw))
    register_op("op.wb.journal_append_to_note", lambda **kw: __import__('work_buddy.journal_backlog.thread_actions', fromlist=['journal_append_to_note']).journal_append_to_note(**kw))
    register_op("op.wb.journal_rewrite_running_notes", lambda **kw: __import__('work_buddy.journal_backlog.rewrite', fromlist=['rewrite_running_notes']).rewrite_running_notes(**kw))


_register()
