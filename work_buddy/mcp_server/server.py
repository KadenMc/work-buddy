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


def _recover_workflow_runs() -> None:
    """Reload incomplete workflow runs from disk at gateway startup.

    The conductor's in-memory active-runs map does not survive a restart;
    without this, every in-flight workflow is silently abandoned and an
    agent's next ``wb_advance`` gets "unknown run". Runs idle past the
    timeout are expired rather than recovered.

    Gated on ``workflows.run_lifecycle.recovery_enabled``. Never raises —
    a recovery failure must not stop the gateway from booting.
    """
    logger = logging.getLogger("work_buddy.mcp_server")
    try:
        from work_buddy.config import load_config
        rl = load_config().get("workflows", {}).get("run_lifecycle", {})
        if not rl.get("recovery_enabled", True):
            logger.info("Workflow run recovery disabled by config — skipping")
            return
        from work_buddy.mcp_server.conductor import recover_active_runs
        result = recover_active_runs()
        logger.info(
            "Workflow run recovery: %d recovered, %d expired",
            len(result.get("recovered", [])),
            len(result.get("expired", [])),
        )
    except Exception as exc:
        logger.warning("Workflow run recovery failed (continuing): %s", exc)


def _start_idle_sweep_in_background() -> None:
    """Periodically sweep idle workflow runs in a daemon thread.

    ``_ACTIVE_RUNS`` lives in this process and an orphaned run never
    leaves it on its own — this sweep reclaims runs idle past the
    configured threshold. Interval and threshold come from
    ``workflows.run_lifecycle`` in config.

    Daemon thread so it never blocks shutdown; each tick is wrapped so a
    failure logs and the loop continues rather than killing the sweep.
    """
    import threading
    import time

    logger = logging.getLogger("work_buddy.mcp_server")

    try:
        from work_buddy.config import load_config
        rl = load_config().get("workflows", {}).get("run_lifecycle", {})
        interval_minutes = float(rl.get("sweep_interval_minutes", 60))
    except Exception:
        interval_minutes = 60.0
    # Clamp to a sane floor — a sub-minute sweep would just churn.
    interval_seconds = max(60.0, interval_minutes * 60.0)

    def _loop() -> None:
        from work_buddy.mcp_server.conductor import sweep_idle_runs
        while True:
            time.sleep(interval_seconds)
            try:
                sweep_idle_runs()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("Idle-run sweep tick failed: %s", exc)

    t = threading.Thread(target=_loop, name="workflow-idle-sweep", daemon=True)
    t.start()
    logger.info(
        "Workflow idle-run sweep started (every %.0f min)",
        interval_seconds / 60.0,
    )


def main_http() -> None:
    """Entry point — run as a persistent streamable-http service."""
    port = _get_port()
    logger = logging.getLogger("work_buddy.mcp_server")
    logger.setLevel(logging.INFO)
    logger.info("Starting MCP gateway (streamable-http) on port %d", port)

    server = _create_server(transport="streamable-http")
    # Each subprocess that may fire FSM transitions on Threads needs
    # its own bootstrap call (the sidecar daemon, the dashboard, and
    # this MCP gateway each have their own module-level state).
    # Without this, spawn capabilities would advance a thread to
    # AWAITING_INFERENCE in this process but the enqueue handler
    # wouldn't be registered, so the thread would dead-end. The
    # shared helper centralizes the boilerplate.
    from work_buddy.threads.bootstrap import bootstrap_for_subprocess
    bootstrap_for_subprocess(subprocess_name="mcp-gateway")
    # Reload in-flight workflow runs from disk before serving requests, so
    # a wb_advance for a pre-restart run resolves instead of dead-ending.
    _recover_workflow_runs()
    _warm_registry_in_background()
    # Background reclaimer for orphaned (idle) workflow runs.
    _start_idle_sweep_in_background()
    server.run(transport="streamable-http")
