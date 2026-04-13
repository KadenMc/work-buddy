"""Subprocess runner for auto_run workflow steps.

Executes a single callable in an isolated process. The conductor spawns
this as ``python -m work_buddy.mcp_server.subprocess_runner`` and
communicates via JSON over stdin/stdout. All logging goes to stderr.

Protocol
--------
**stdin** (JSON object)::

    {
        "callable": "work_buddy.foo.bar",
        "kwargs": {"limit": 1000},
        "session_id": "mcp-abc123"
    }

**stdout** (JSON object)::

    {"success": true, "value": {"count": 42}}
    {"success": false, "error": "ImportError: ...", "traceback": "..."}

Stderr carries diagnostic logging — never parsed by the conductor.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("work_buddy.mcp_server.subprocess_runner")


def _safe_serialize(obj: Any) -> Any:
    """Make an object JSON-safe (mirrors conductor._safe_serialize)."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_serialize(v) for v in obj]
    return str(obj)


def main() -> None:
    """Read spec from stdin, call the function, write result to stdout."""
    # --- Protect stdout from pollution ---
    # Save the real stdout for writing the JSON result, then redirect
    # sys.stdout → stderr. This prevents ANY imported module (especially
    # work_buddy.logging_config, which attaches a StreamHandler(sys.stdout)
    # to the work_buddy root logger) from contaminating the JSON channel.
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr

    def _write_result(result: dict[str, Any]) -> None:
        """Write JSON result to the real stdout and flush."""
        json.dump(result, _real_stdout)
        _real_stdout.flush()

    # All logging to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s: %(name)s: %(message)s",
    )

    raw = sys.stdin.read()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        _write_result({"success": False, "error": f"Invalid JSON input: {exc}"})
        sys.exit(1)

    # Set session ID so work_buddy logging infrastructure works.
    session_id = spec.get("session_id", "")
    if session_id:
        os.environ["WORK_BUDDY_SESSION_ID"] = session_id

    dotted_path = spec.get("callable", "")
    kwargs = spec.get("kwargs", {})

    logger.info("subprocess_runner: executing %s", dotted_path)

    # --- Validate import path ---
    if not dotted_path.startswith("work_buddy."):
        _write_result({
            "success": False,
            "error": f"Import path {dotted_path!r} rejected: only work_buddy.* allowed",
        })
        sys.exit(1)

    parts = dotted_path.rsplit(".", 1)
    if len(parts) != 2:
        _write_result({
            "success": False,
            "error": f"Invalid callable path {dotted_path!r}: expected module.function",
        })
        sys.exit(1)

    module_path, func_name = parts

    # --- Import module ---
    try:
        mod = importlib.import_module(module_path)
    except ImportError as exc:
        _write_result({
            "success": False,
            "error": f"ImportError for {module_path!r}: {exc}",
            "traceback": traceback.format_exc(),
        })
        sys.exit(1)

    func = getattr(mod, func_name, None)
    if func is None or not callable(func):
        _write_result({
            "success": False,
            "error": f"{func_name!r} not found or not callable in {module_path!r}",
        })
        sys.exit(1)

    # --- Execute ---
    try:
        value = func(**kwargs)
        _write_result({"success": True, "value": _safe_serialize(value)})
        logger.info("subprocess_runner: %s -> success", dotted_path)
    except Exception as exc:
        _write_result({
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })
        logger.error("subprocess_runner: %s -> failed: %s", dotted_path, exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
