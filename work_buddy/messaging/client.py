"""Python client for the messaging service.

Used by work-buddy internally. Auto-starts the service if it's not
running when work-buddy calls it.
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent


def _base_url(cfg: dict[str, Any] | None = None) -> str:
    if cfg is None:
        cfg = load_config()
    port = cfg.get("messaging", {}).get("service_port", 5123)
    return f"http://localhost:{port}"


def _ensure_service_running() -> bool:
    """Start the messaging service if it's not already running.

    Returns True if the service is reachable after this call.
    """
    if is_service_running():
        return True

    logger.info("Messaging service not running — auto-starting...")
    try:
        from work_buddy.compat import conda_activate_command, detached_process_kwargs
        cmd = conda_activate_command(str(_REPO_ROOT), "work_buddy.messaging.service")
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detached_process_kwargs(),
        )
    except OSError as exc:
        logger.warning("Failed to auto-start messaging service: %s", exc)
        return False

    # Wait for it to come up
    for _ in range(10):
        time.sleep(0.5)
        if is_service_running():
            logger.info("Messaging service started successfully.")
            return True

    logger.warning("Messaging service did not start within 5 seconds.")
    return False


def _request(method: str, path: str, data: dict | None = None) -> dict | None:
    """Make a request to the messaging service. Auto-starts if needed."""
    url = f"{_base_url()}{path}"
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=5) as resp:
            if resp.status == 204:
                return None
            return json.loads(resp.read().decode())
    except URLError:
        # Service might be down — try auto-starting (skip for health checks)
        if path != "/health" and _ensure_service_running():
            try:
                req2 = Request(url, data=body, method=method)
                req2.add_header("Content-Type", "application/json")
                with urlopen(req2, timeout=5) as resp:
                    if resp.status == 204:
                        return None
                    return json.loads(resp.read().decode())
            except URLError:
                return None
        return None


def is_service_running() -> bool:
    """Check if the messaging service is reachable (does NOT auto-start)."""
    url = f"{_base_url()}/health"
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            return data.get("status") == "ok"
    except (URLError, Exception):
        return False


def send_message(
    *,
    sender: str,
    recipient: str,
    type: str,
    subject: str,
    body: str | None = None,
    sender_session: str | None = None,
    recipient_session: str | None = None,
    thread_id: str | None = None,
    priority: str = "normal",
    tags: list[str] | None = None,
) -> dict | None:
    """Send a message via the HTTP service."""
    payload: dict[str, Any] = {
        "sender": sender,
        "recipient": recipient,
        "type": type,
        "subject": subject,
    }
    if body is not None:
        payload["body"] = body
    if sender_session:
        payload["sender_session"] = sender_session
    if recipient_session:
        payload["recipient_session"] = recipient_session
    if thread_id:
        payload["thread_id"] = thread_id
    if priority != "normal":
        payload["priority"] = priority
    if tags:
        payload["tags"] = tags

    return _request("POST", "/messages", payload)


def query_messages(
    *,
    recipient: str | None = None,
    session: str | None = None,
    status: str | None = None,
    sender: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query messages from the service."""
    params = []
    if recipient:
        params.append(f"recipient={recipient}")
    if session:
        params.append(f"session={session}")
    if status:
        params.append(f"status={status}")
    if sender:
        params.append(f"sender={sender}")
    params.append(f"limit={limit}")

    qs = "&".join(params)
    result = _request("GET", f"/messages?{qs}")
    if result is None:
        return []
    return result.get("messages", [])


def read_message(
    msg_id: str,
    session: str | None = None,
    reader_project: str | None = None,
) -> dict | None:
    """Fetch a single message with full body content."""
    params = []
    if session:
        params.append(f"session={session}")
    if reader_project:
        params.append(f"reader_project={reader_project}")
    qs = ("?" + "&".join(params)) if params else ""
    return _request("GET", f"/messages/{msg_id}{qs}")


def update_status(msg_id: str, new_status: str) -> dict | None:
    """Update a message's status."""
    return _request("PATCH", f"/messages/{msg_id}", {"status": new_status})


def get_thread(thread_id: str) -> list[dict]:
    """Get all messages in a thread."""
    result = _request("GET", f"/threads/{thread_id}")
    if result is None:
        return []
    return result.get("messages", [])


def reply(
    msg_id: str,
    *,
    sender: str,
    body: str,
    sender_session: str | None = None,
    type: str = "ack",
) -> dict | None:
    """Reply to an existing message."""
    payload: dict[str, Any] = {"sender": sender, "body": body, "type": type}
    if sender_session:
        payload["sender_session"] = sender_session
    return _request("POST", f"/messages/{msg_id}/reply", payload)
