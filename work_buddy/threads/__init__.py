"""Thread chat system — multi-turn conversations between agents and users.

Threads are a standalone subsystem. Any part of work-buddy can open a thread
to have a back-and-forth with the user (notifications are the first consumer).

Usage via MCP gateway::

    wb_run("thread_create", {"title": "Planning task", "message": "Here's what I'll do..."})
    wb_run("thread_send",   {"thread_id": "...", "message": "Step 1 complete."})
    wb_run("thread_ask",    {"thread_id": "...", "question": "Proceed?", "response_type": "boolean"})
    wb_run("thread_poll",   {"thread_id": "..."})
    wb_run("thread_close",  {"thread_id": "..."})
"""
