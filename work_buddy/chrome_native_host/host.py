#!/usr/bin/env python3
"""Native messaging host for the Work Buddy Tab Exporter Chrome extension.

Handles three message types:
- action="check": check if a .chrome_tabs_request file exists (lightweight)
- action="export" (or no action): receive tab data and write .chrome_tabs.json
- action="periodic_snapshot": append a tab snapshot to the rolling ledger

The request file is created by the Python collector when it needs fresh tab data.
The periodic_snapshot action is sent by the extension's background alarm every
5 minutes for temporal tab tracking.
"""

from __future__ import annotations

import json
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from work_buddy.paths import resolve

OUTPUT_FILE = resolve("cache/chrome-tabs")
REQUEST_FILE = resolve("cache/chrome-request")
LEDGER_FILE = resolve("chrome/ledger")
LEDGER_WINDOW_DAYS = 7


def read_message() -> dict:
    """Read a single native messaging message from stdin."""
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) < 4:
        sys.exit(0)

    message_length = struct.unpack("<I", raw_length)[0]
    raw_message = sys.stdin.buffer.read(message_length)

    if len(raw_message) < message_length:
        sys.exit(0)

    return json.loads(raw_message.decode("utf-8"))


def write_message(message: dict) -> None:
    """Write a single native messaging message to stdout."""
    encoded = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _read_request_params() -> dict:
    """Read request parameters from the request file.

    The request file may contain:
    - A plain ISO timestamp (legacy format)
    - A JSON object with keys like ``requested_at``, ``since``, ``until``,
      ``request_action``, ``tab_ids``, ``max_chars``
    """
    if not REQUEST_FILE.exists():
        return {}
    try:
        raw = REQUEST_FILE.read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            return json.loads(raw)
        # Legacy: plain ISO timestamp
        return {"requested_at": raw}
    except (json.JSONDecodeError, OSError):
        return {}


def handle_check() -> None:
    """Check if a tab export has been requested.

    If a request file exists, also reads parameters and passes them
    to the extension (since/until for history, tab_ids for content, etc.).
    """
    requested = REQUEST_FILE.exists()
    response = {"status": "ok", "requested": requested}

    if requested:
        params = _read_request_params()
        # Pass through all known request parameters to the extension
        for key in ("since", "until", "request_action", "tab_ids", "max_chars",
                    "mutation", "title", "color", "group_id", "window_id", "index",
                    "url", "target_hash"):
            if params.get(key) is not None:
                response[key] = params[key]

    write_message(response)


def handle_periodic_snapshot(message: dict) -> None:
    """Append a periodic tab snapshot to the rolling ledger.

    This is called by the extension's background alarm (every 5 minutes).
    Fully self-contained with stdlib only — no work_buddy imports — because
    Chrome launches the native host with system Python, not the conda env.
    """
    try:
        # Build snapshot from message
        snapshot = {
            "captured_at": message.get("captured_at", datetime.now(timezone.utc).isoformat()),
            "tabs": message.get("tabs", []),
            "tab_count": message.get("tab_count", len(message.get("tabs", []))),
        }
        history = message.get("history")
        if history:
            snapshot["history"] = history
            snapshot["history_count"] = len(history)

        # Read existing ledger
        snapshots = []
        if LEDGER_FILE.exists():
            try:
                data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    snapshots = data
                elif isinstance(data, dict):
                    snapshots = data.get("snapshots", [])
            except (json.JSONDecodeError, OSError):
                snapshots = []

        # Append and prune old entries
        snapshots.append(snapshot)
        cutoff = (datetime.now() - timedelta(days=LEDGER_WINDOW_DAYS)).isoformat()
        snapshots = [s for s in snapshots if s.get("captured_at", "") >= cutoff]

        # Write atomically
        temp = LEDGER_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(snapshots, ensure_ascii=False), encoding="utf-8")
        temp.replace(LEDGER_FILE)

        write_message({"status": "ok", "ledger_count": len(snapshots)})
    except Exception as exc:
        write_message({"status": "error", "error": str(exc)})


def handle_export(message: dict) -> None:
    """Write tab snapshot to disk and clean up request file."""
    message["host_written_at"] = datetime.now(timezone.utc).isoformat()

    # Remove the action field before writing
    message.pop("action", None)

    # Write atomically
    temp_file = OUTPUT_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(message, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_file.replace(OUTPUT_FILE)

    # Clean up request file
    if REQUEST_FILE.exists():
        REQUEST_FILE.unlink()

    write_message({
        "status": "ok",
        "tabs_received": message.get("tab_count", 0),
        "written_to": str(OUTPUT_FILE),
    })


def main() -> None:
    """Read a message and route to the appropriate handler."""
    try:
        message = read_message()
        action = message.get("action", "export")

        if action == "check":
            handle_check()
        elif action == "periodic_snapshot":
            handle_periodic_snapshot(message)
        else:
            handle_export(message)

    except Exception as exc:
        try:
            write_message({"status": "error", "error": str(exc)})
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
