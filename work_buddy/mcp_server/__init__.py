"""work-buddy MCP gateway server.

Exposes work-buddy capabilities via 4 meta-tools:
  wb_search   — discover capabilities and workflows
  wb_run      — execute a function or start a workflow
  wb_advance  — advance a running workflow to its next step
  wb_status   — check workflow progress or system health
"""

from work_buddy.mcp_server.server import main, main_http

__all__ = ["main", "main_http"]
