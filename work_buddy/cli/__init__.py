"""work-buddy ``wb`` command-line interface.

The shell entrypoint for setup and sidecar lifecycle. See
``work_buddy.cli.dispatch`` for the command surface.
"""

import os

# `wb` runs standalone (a plain user shell, not a Claude Code session), so it
# needs a synthetic session id for work_buddy's logging system before any
# import that resolves the session dir. Mirrors the service entry points
# (dashboard / embedding / mcp_server). setdefault yields to a real agent
# session id when an agent shells out to `wb`.
os.environ.setdefault("WORK_BUDDY_SESSION_ID", "wb-cli")

from work_buddy.cli.dispatch import main

__all__ = ["main"]
