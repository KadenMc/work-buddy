"""python -m work_buddy.sidecar — start the sidecar daemon."""

# First, before anything can start a process: give the daemon its host
# context. Started by the logon task or a desktop shortcut, the daemon runs
# under pythonw.exe with no console, and every console program it later ran
# without the no-window flag would open a window. This gives it a console
# with no window, and a sidecar-owned session id of its own. That id is the
# sidecar principal's consent session, and it is never inherited from
# whoever started the daemon. It must only be consulted for consent through
# ``consent_principal.sidecar_self()``: see the ``notifications/consent``
# knowledge unit, "The three consent principals".
from work_buddy.process import HostRole, establish_host_context

establish_host_context(HostRole.SIDECAR)

import sys

from work_buddy.sidecar.daemon import run


def main() -> None:
    foreground = "--foreground" in sys.argv or "-f" in sys.argv
    run(foreground=foreground)


main()
