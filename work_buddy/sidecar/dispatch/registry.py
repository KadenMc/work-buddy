"""Agent spawn registry — durable store for persistent agent sessions.

Tracks spawned agents so they can be discovered, resumed, and eventually
routed to by the semantic routing layer (Phase C). This is the seed of
the sidecar's "addressable agent" model.

Storage: a single ``agent_registry.json`` file at the repo root, alongside
``sidecar_state.json``. Each entry is keyed by session_id. The file is
small (tens of entries at most) so full-file read/write is fine.

Only ``headless_persistent`` and ``interactive_persistent`` spawns are
registered. Ephemeral spawns are fire-and-forget by definition.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.paths import resolve
from work_buddy.sidecar.dispatch.models import SpawnMode, SpawnResult

logger = get_logger(__name__)

REGISTRY_FILE = resolve("runtime/agent-registry")


def register_agent(result: SpawnResult) -> None:
    """Write a spawn result to the registry.

    Only registers persistent modes (where session_id is meaningful).
    Silently returns for ephemeral spawns or missing session_id.
    """
    if not result.session_id:
        logger.debug("No session_id — skipping registry write.")
        return

    mode = SpawnMode(result.spawn_mode)
    if not mode.is_persistent:
        logger.debug("Ephemeral spawn — skipping registry write.")
        return

    registry = _load_registry()
    registry[result.session_id] = result.to_dict()
    _save_registry(registry)

    logger.info(
        "Registered agent: session=%s, mode=%s, job=%s",
        result.session_id, result.spawn_mode, result.source_job,
    )


def get_agent(session_id: str) -> SpawnResult | None:
    """Look up an agent by its Claude session ID."""
    registry = _load_registry()
    data = registry.get(session_id)
    if data is None:
        return None
    return SpawnResult.from_dict(data)


def update_agent(session_id: str, **updates: Any) -> SpawnResult | None:
    """Update fields on a registered agent.

    Common updates: status, last_resumed_at, notes.
    Returns the updated SpawnResult, or None if not found.
    """
    registry = _load_registry()
    data = registry.get(session_id)
    if data is None:
        return None

    data.update(updates)
    registry[session_id] = data
    _save_registry(registry)

    return SpawnResult.from_dict(data)


def list_agents(
    *,
    status: str | None = None,
    spawn_mode: str | None = None,
    source_job: str | None = None,
) -> list[SpawnResult]:
    """List registered agents with optional filters."""
    registry = _load_registry()
    results = []
    for data in registry.values():
        if status and data.get("status") != status:
            continue
        if spawn_mode and data.get("spawn_mode") != spawn_mode:
            continue
        if source_job and data.get("source_job") != source_job:
            continue
        results.append(SpawnResult.from_dict(data))
    return results


def mark_resumed(session_id: str) -> SpawnResult | None:
    """Mark an agent as resumed with a timestamp."""
    from datetime import datetime, timezone

    return update_agent(
        session_id,
        status="resumed",
        last_resumed_at=datetime.now(timezone.utc).isoformat(),
    )


def remove_agent(session_id: str) -> bool:
    """Remove an agent from the registry. Returns True if found."""
    registry = _load_registry()
    if session_id not in registry:
        return False
    del registry[session_id]
    _save_registry(registry)
    logger.info("Removed agent from registry: session=%s", session_id)
    return True


# ---------------------------------------------------------------------------
# Internal I/O
# ---------------------------------------------------------------------------

def _load_registry() -> dict[str, Any]:
    """Load the registry from disk. Returns empty dict if missing/corrupt."""
    if not REGISTRY_FILE.exists():
        return {}
    try:
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load agent registry: %s", exc)
        return {}


def _save_registry(registry: dict[str, Any]) -> None:
    """Atomically write the registry to disk."""
    fd, tmp_path = tempfile.mkstemp(
        dir=REGISTRY_FILE.parent,
        prefix=".agent_registry_",
        suffix=".tmp",
    )
    try:
        os.write(fd, json.dumps(registry, indent=2).encode())
        os.close(fd)
        os.replace(tmp_path, REGISTRY_FILE)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
