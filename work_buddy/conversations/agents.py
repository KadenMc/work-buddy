"""Mapping from conversation_id → driving agent process.

When a feature opens a chat-driven walkthrough (Jobs help, future
contracts/projects help, etc.) it spawns a headless ``claude --print``
subprocess that drives the conversation. Register the (conversation_id,
pid) pairing here so the dashboard's ``GET /api/conversations/<id>``
endpoint can report whether the agent process is still alive.

That liveness signal is load-bearing: the chat sidebar uses it to
distinguish "agent is thinking" from "agent stopped responding"
(budget exceeded / crashed / killed). Without it, the typing
indicator either dances forever after a dead agent (visible bug) or
relies on a time-based guess.

Storage is process-local in-memory. The dashboard process registers
on chat-spawn and queries on every conversation fetch — same
process, same memory, no IPC. If a feature spawns from elsewhere
(future), it should POST a registration through the dashboard.
"""

from __future__ import annotations

import threading
from typing import Optional


_lock = threading.Lock()
_pids: dict[str, int] = {}


def register(conversation_id: str, pid: int) -> None:
    """Record the agent process driving this conversation."""
    if not conversation_id or not pid:
        return
    with _lock:
        _pids[conversation_id] = int(pid)


def unregister(conversation_id: str) -> None:
    """Forget a conversation's agent. Called on conversation_close."""
    with _lock:
        _pids.pop(conversation_id, None)


def get_pid(conversation_id: str) -> Optional[int]:
    """Return the registered driving PID, or None if never registered."""
    with _lock:
        return _pids.get(conversation_id)


def is_alive(conversation_id: str) -> Optional[bool]:
    """Return whether the driving agent process is alive.

    Returns ``True``/``False`` if a pid was registered for this
    conversation, ``None`` if no agent was ever registered (e.g.
    a user-driven conversation with no spawned driver).
    """
    pid = get_pid(conversation_id)
    if pid is None:
        return None
    # Lazy import — sidecar.pid pulls in platform-specific stuff.
    from work_buddy.sidecar.pid import _is_process_alive
    return _is_process_alive(pid)
