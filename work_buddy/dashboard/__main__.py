"""Allow running as: python -m work_buddy.dashboard"""

import os
import uuid

if not os.environ.get("WORK_BUDDY_SESSION_ID"):
    os.environ["WORK_BUDDY_SESSION_ID"] = f"dashboard-{uuid.uuid4().hex[:8]}"

from work_buddy.dashboard.service import main

main()
