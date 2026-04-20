"""Built-in inline command handlers.

Importing any submodule triggers its ``@inline_command`` decorator and
registers the handler in :mod:`work_buddy.inline.registry`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from . import task_new  # noqa: F401
except Exception as exc:  # noqa: BLE001
    logger.warning("inline.handlers.task_new import failed: %s", exc)
