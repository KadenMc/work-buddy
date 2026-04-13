"""python -m work_buddy.sidecar — start the sidecar daemon."""

import os
import sys
import uuid

# The sidecar runs standalone (not inside a Claude Code session),
# so it needs a synthetic session ID for work_buddy's logging system.
if not os.environ.get("WORK_BUDDY_SESSION_ID"):
    os.environ["WORK_BUDDY_SESSION_ID"] = f"sidecar-{uuid.uuid4().hex[:8]}"

from work_buddy.sidecar.daemon import run


def main() -> None:
    foreground = "--foreground" in sys.argv or "-f" in sys.argv
    run(foreground=foreground)


main()
