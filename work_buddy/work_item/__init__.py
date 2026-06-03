"""work_buddy.work_item — neutral home for cross-subtype WorkItem machinery.

Holds the task write port (:mod:`work_buddy.work_item.task_adapter`) — the
delegate-first bridge a ``Task`` uses to reach the task mutation layer. The
``WorkItem`` base and the ``Task`` / ``Thread`` models live in
``work_buddy.threads``.
"""

from work_buddy.work_item import task_adapter

__all__ = ["task_adapter"]
