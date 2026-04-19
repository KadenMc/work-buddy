"""work-buddy MCP gateway server.

Creates a FastMCP server with 5 gateway tools. Supports two transports:

- **stdio** (default): spawned by Claude Code per-session. JSON-RPC over
  stdin/stdout — all logging MUST go to stderr.
- **streamable-http**: persistent sidecar service on a configured port.
  Survives across sessions, no cold-start penalty.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid

from mcp.server.fastmcp import FastMCP

from work_buddy.config import load_config

# The MCP server subprocess needs a session ID for work_buddy's logging
# system. Set a synthetic one if not already present.
if not os.environ.get("WORK_BUDDY_SESSION_ID"):
    os.environ["WORK_BUDDY_SESSION_ID"] = f"mcp-{uuid.uuid4().hex[:8]}"

# Force ALL logging to stderr BEFORE any other imports that might log
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
    force=True,
)

_DEFAULT_PORT = 5126


def _get_port() -> int:
    """Resolve the MCP server port from config or default."""
    try:
        cfg = load_config()
        return cfg.get("sidecar", {}).get("services", {}).get(
            "mcp_gateway", {}
        ).get("port", _DEFAULT_PORT)
    except Exception:
        return _DEFAULT_PORT


def _create_server(*, transport: str = "stdio") -> FastMCP:
    """Create and configure the FastMCP server instance.

    Args:
        transport: "stdio" for per-session or "streamable-http" for persistent.
    """
    kwargs: dict = {
        "name": "work-buddy",
        "instructions": (
            "work-buddy MCP gateway with dynamic tool discovery. "
            "Use wb_search to discover capabilities (returns parameter schemas), "
            "wb_run to execute them, wb_advance to step through workflows, "
            "and wb_status to check progress. "
            "Always call wb_search first if unsure what parameters a capability accepts."
        ),
    }

    if transport == "streamable-http":
        port = _get_port()
        kwargs.update(host="127.0.0.1", port=port, log_level="WARNING")

    mcp = FastMCP(**kwargs)

    # Register the 5 gateway tools
    from work_buddy.mcp_server.tools.gateway import register_tools
    register_tools(mcp)

    # Health-check endpoint for the sidecar supervisor (streamable-http only,
    # but registering it unconditionally is harmless).
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return mcp


def main() -> None:
    """Entry point — create server and run on stdio (legacy / fallback)."""
    server = _create_server(transport="stdio")
    server.run(transport="stdio")


def _warm_registry_in_background() -> None:
    """Materialize the registry in a daemon thread so the first real tool
    call finds it already built.

    Rationale: registry build takes several seconds (tool probes, workflow
    loading, knowledge index warm). Historically the first ``wb_search``
    or ``wb_run`` after a cold gateway boot paid that whole cost
    synchronously. Even with ``asyncio.to_thread`` protecting the event
    loop, the user still waits. Building eagerly removes that latency.

    This is belt-and-suspenders: if a future handler regresses on
    async-thread hygiene (see architecture/mcp-import-discipline), the
    registry will already be built, so nothing blocks the event loop
    anyway. Safe under all failure modes — if the build fails, the next
    ``get_registry()`` call rebuilds exactly as before.
    """
    import threading
    from work_buddy.mcp_server.registry import get_registry

    logger = logging.getLogger("work_buddy.mcp_server")

    def _build():
        try:
            get_registry()
        except Exception as exc:
            logger.warning("Background registry warm-up failed: %s", exc)

    t = threading.Thread(target=_build, name="registry-warm", daemon=True)
    t.start()


def main_http() -> None:
    """Entry point — run as a persistent streamable-http service."""
    port = _get_port()
    logger = logging.getLogger("work_buddy.mcp_server")
    logger.setLevel(logging.INFO)
    logger.info("Starting MCP gateway (streamable-http) on port %d", port)

    server = _create_server(transport="streamable-http")
    _warm_registry_in_background()
    server.run(transport="streamable-http")
