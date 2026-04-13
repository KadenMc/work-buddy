"""Entry point for ``python -m work_buddy.mcp_server``.

Supports two modes:
  python -m work_buddy.mcp_server          # stdio (default, per-session)
  python -m work_buddy.mcp_server --http   # streamable-http (persistent sidecar service)
"""

import sys

if "--http" in sys.argv:
    from work_buddy.mcp_server.server import main_http
    main_http()
else:
    from work_buddy.mcp_server.server import main
    main()
