"""Async fire-and-forget LLM submission.

``llm_submit`` writes an operation record marked ``queued=true`` /
``queue_reason="deferred_submit"`` and returns immediately with the
operation id. The sidecar's retry sweep picks it up on its next tick,
invokes ``llm_call`` with the given params (via the registry), and
messages the originating agent session on completion.

This uses the same on-disk op substrate as the retry queue — not a
parallel queue — so leases, messaging callbacks, workflow resume, and
per-session cost-log routing all work for free. The ``queue_reason``
field lets the sweep apply policy that differs from retry (no backoff,
quiet failure).

For synchronous bounded calls, use ``llm_call``. Use ``llm_submit``
only when the caller doesn't want to wait for local inference latency.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any


def _operations_dir():
    from work_buddy.paths import data_dir
    d = data_dir("agents") / "operations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def llm_submit(
    *,
    system: str,
    user: str,
    profile: str,
    output_schema: dict | str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    cache_ttl_minutes: int | None = None,
) -> dict[str, Any]:
    """Queue an ``llm_call`` for async background execution.

    Args:
        system: System prompt (same shape as ``llm_call``).
        user: User message.
        profile: Named profile (e.g. ``"local_general"``) — required;
            submitting a cloud tier for async makes little sense because
            cloud calls are already fast. Use ``llm_call`` for those.
        output_schema: Optional JSON Schema for structured output.
        max_tokens: Max response tokens.
        temperature: Sampling temperature.
        cache_ttl_minutes: Cache TTL override.

    Returns:
        ``{operation_id, status: "queued", profile, queue_reason,
        queued_at, estimated_start_within_seconds, hint}``

        The ``hint`` field explains to the caller (agent or human) how
        to retrieve the result via ``wb_status`` and what to expect.
    """
    if not profile:
        return {
            "error": (
                "'profile' is required for llm_submit. Use a local "
                "profile declared under llm.profiles in config. "
                "Cloud tier calls are already fast — use llm_call instead."
            ),
        }

    now = datetime.now(timezone.utc)
    op_id = f"op_{uuid.uuid4().hex[:8]}"
    originating_session_id = os.environ.get(
        "WORK_BUDDY_SESSION_ID", "unknown",
    )

    # Params that retry_sweep._replay() will pass to llm_call().
    replay_params: dict[str, Any] = {
        "system": system,
        "user": user,
        "profile": profile,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if output_schema is not None:
        replay_params["output_schema"] = output_schema
    if cache_ttl_minutes is not None:
        replay_params["cache_ttl_minutes"] = cache_ttl_minutes

    record: dict[str, Any] = {
        "operation_id": op_id,
        "type": "capability",
        "name": "llm_call",
        "params": replay_params,
        "retry_policy": "replay",
        # status="failed" is the sweep's gate; `queued=True` and
        # retry_at<=now together make the record immediately pickable.
        "status": "failed",
        "result": None,
        "error": None,
        "attempt": 0,
        "session_id": originating_session_id,
        "originating_session_id": originating_session_id,
        "locked_until": None,
        "created_at": now.isoformat(),
        "completed_at": None,
        # Queue fields
        "queued": True,
        "queue_reason": "deferred_submit",
        "queued_for_retry": True,  # legacy alias so older sweeps pick it up
        "retry_at": now.isoformat(),
        "max_retries": 1,  # one attempt; deferred submits don't retry on real failure
        "backoff_strategy": "none",
        "lease_seconds": 600,  # long lease — local inference can take minutes
        "retry_history": [],
    }

    path = _operations_dir() / f"{op_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)

    return {
        "operation_id": op_id,
        "status": "queued",
        "profile": profile,
        "queue_reason": "deferred_submit",
        "queued_at": now.isoformat(),
        "estimated_start_within_seconds": 10,
        "hint": (
            f"Job queued. You'll receive a messaging notification when it "
            f"completes (usually in your next turn). To check status "
            f"manually: wb_run('wb_status', {{'operation_id': '{op_id}'}}). "
            f"When status='completed', the LLM output is at .result.content "
            f"(or .result.parsed for structured output). "
            f"If you need the result synchronously, use llm_call instead."
        ),
    }
