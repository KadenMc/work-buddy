"""work-buddy status CLI — a read-only, shell-pollable window into
consent-request and operation status for tooling that cannot speak MCP
(the ``Monitor`` tool, ``bash`` background loops, cron).

Invoke as ``python -m work_buddy.statusctl``. See :mod:`work_buddy.statusctl.cli`.
"""

from work_buddy.statusctl.cli import main

__all__ = ["main"]
