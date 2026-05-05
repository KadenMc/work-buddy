"""Conversation system — multi-turn agent-user dialogue.

Conversations are a standalone subsystem. Any part of work-buddy can open
a conversation to have a back-and-forth with the user (notifications are
the first consumer).

Renamed from ``work_buddy.threads``; that namespace is reserved for
the universal-entity primitive (see :mod:`work_buddy.threads`).

Usage via MCP gateway::

    wb_run("conversation_create", {"title": "Planning task", "message": "Here's what I'll do..."})
    wb_run("conversation_send",   {"conversation_id": "...", "message": "Step 1 complete."})
    wb_run("conversation_ask",    {"conversation_id": "...", "question": "Proceed?", "response_type": "boolean"})
    wb_run("conversation_poll",   {"conversation_id": "..."})
    wb_run("conversation_close",  {"conversation_id": "..."})
"""
