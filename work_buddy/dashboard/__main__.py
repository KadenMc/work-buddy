"""Allow running as: python -m work_buddy.dashboard"""

import os
import uuid
import sys

if not os.environ.get("WORK_BUDDY_SESSION_ID"):
    os.environ["WORK_BUDDY_SESSION_ID"] = f"dashboard-{uuid.uuid4().hex[:8]}"

if "--read-only" in sys.argv:
    import argparse
    from work_buddy.dashboard.read_only import serve
    parser = argparse.ArgumentParser(description="Serve an isolated read-only dashboard process.")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    serve(port=args.port)
else:
    from work_buddy.dashboard.service import main
    main()
