"""python -m work_buddy.embedding — start the shared embedding service."""

import os
import uuid

# The embedding service runs standalone (not inside a Claude Code session),
# so it needs a synthetic session ID for work_buddy's logging system.
if not os.environ.get("WORK_BUDDY_SESSION_ID"):
    os.environ["WORK_BUDDY_SESSION_ID"] = f"embed-{uuid.uuid4().hex[:8]}"

from work_buddy.embedding.service import main

main()
