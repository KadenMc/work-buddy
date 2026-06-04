"""User-facing remediation hints for work-buddy services.

Single-sourced so the "how to restart" text can't drift between the health
subsystem and ad-hoc error messages — a hardcoded copy can rot into naming a
scheduled task that doesn't exist. The embedding, messaging, and dashboard
services are children supervised by the sidecar daemon; restarting the sidecar
is the correct remediation for any of them, and only the sidecar itself is a
scheduled task.
"""

from __future__ import annotations

import sys


def sidecar_restart_command() -> str:
    """OS-aware shell command to restart the sidecar daemon.

    Returns the bare command (no prose prefix) so callers can embed it in
    whatever sentence fits their surface.
    """
    if sys.platform == "win32":
        return "Start-ScheduledTask 'WB-Sidecar'"
    return "python -m work_buddy.sidecar &"
