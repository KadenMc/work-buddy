"""Email-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field). The closure code below
is moved verbatim from the former ``registry.py`` builder.
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op



def _register() -> None:
    """Capabilities exposed by the email/Thunderbird integration.

    All callables flow through ``work_buddy.email.provider.get_email_provider``,
    which currently returns the Thunderbird HTTP client. The ``thunderbird``
    tool probe gates these so they're filtered out of the registry when the
    bridge isn't reachable.
    """
    from work_buddy.email.capabilities import (
        email_accounts,
        email_display,
        email_get,
        email_health,
    )

    register_op("op.wb.email_health", email_health)
    register_op("op.wb.email_accounts", email_accounts)
    register_op("op.wb.email_get", email_get)
    register_op("op.wb.email_display", email_display)
    register_op("op.wb.email_close", lambda **kw: __import__('work_buddy.email.thread_actions', fromlist=['email_close']).email_close(**kw))
    register_op("op.wb.email_create_tasks", lambda **kw: __import__('work_buddy.email.thread_actions', fromlist=['email_create_tasks']).email_create_tasks(**kw))
    register_op("op.wb.email_create_umbrella_task", lambda **kw: __import__('work_buddy.email.thread_actions', fromlist=['email_create_umbrella_task']).email_create_umbrella_task(**kw))
    register_op("op.wb.email_record_into_task", lambda **kw: __import__('work_buddy.email.thread_actions', fromlist=['email_record_into_task']).email_record_into_task(**kw))


_register()
