"""Retry sweep — background processor for queued-for-retry operations.

Called by the sidecar daemon on each tick. Scans operation records for
failed operations that are queued for retry, replays them using the
capability registry, and notifies the originating agent session on
success or exhaustion.

Design notes:
- The sidecar runs in its own process and CAN import heavy libs.
- Operations are replayed via ``entry.callable(**params)`` directly,
  not through MCP tools (no asyncio, no gateway overhead).
- Notifications use the messaging service for agent delivery and
  the notification dispatcher for user-facing alerts on exhaustion.
- Workflow integration: on success, if the operation was part of a
  workflow DAG, the conductor is asked to resume the suspended step.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


class RetrySweep:
    """Scans operation records for queued retries and replays them.

    Only processes operations where:
    - queued_for_retry == True
    - status == "failed"
    - retry_at <= now
    - attempt < max_retries
    - locked_until is None or expired (prevent double-dispatch)
    """

    def __init__(self, config: dict[str, Any] | None = None, event_log: Any | None = None) -> None:
        self._event_log = event_log
        self._config = config or {}
        rq = self._config.get("sidecar", {}).get("retry_queue", {})
        self._enabled = rq.get("enabled", True)
        self._max_age_minutes = rq.get("max_retry_age_minutes", 30)

    def sweep(self) -> list[dict[str, Any]]:
        """Scan and process all ready-to-retry operations.

        Returns list of ``{op_id, capability, success, error?, attempt}``
        for logging / observability.
        """
        if not self._enabled:
            return []

        ops_dir = _get_operations_dir()
        if not ops_dir.exists():
            return []

        now = datetime.now(timezone.utc)
        results: list[dict[str, Any]] = []

        for path in ops_dir.glob("op_*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            if not self._is_ready(record, now):
                continue

            # Acquire lease (prevent concurrent execution)
            record["status"] = "running"
            record["locked_until"] = (now + timedelta(seconds=90)).isoformat()
            record["attempt"] = record.get("attempt", 1) + 1
            _write_record(record)

            if self._event_log:
                self._event_log.emit(
                    "retry_attempt", record["operation_id"],
                    f"Retrying {record['name']} (attempt {record['attempt']})",
                )

            # Execute
            replay_result = self._replay(record)
            replay_result["op_id"] = record["operation_id"]
            replay_result["capability"] = record["name"]
            replay_result["attempt"] = record["attempt"]
            results.append(replay_result)

            if replay_result["success"]:
                self._on_success(record, replay_result)
                if self._event_log:
                    self._event_log.emit(
                        "retry_succeeded", record["operation_id"],
                        f"Retry OK: {record['name']} (attempt {record['attempt']})",
                    )
            elif record["attempt"] >= record.get("max_retries", 5):
                self._on_exhausted(record, replay_result)
                if self._event_log:
                    self._event_log.emit(
                        "retry_exhausted", record["operation_id"],
                        f"Retry exhausted: {record['name']} after {record['attempt']} attempts",
                        level="warning",
                    )
            else:
                self._schedule_next(record, replay_result.get("error", "unknown"))
                if self._event_log:
                    self._event_log.emit(
                        "retry_scheduled", record["operation_id"],
                        f"Retry failed, next at {record.get('retry_at')}: {record['name']}",
                    )

        return results

    def _is_ready(self, record: dict[str, Any], now: datetime) -> bool:
        """Check whether this operation should be retried now."""
        if not record.get("queued_for_retry"):
            return False
        if record.get("status") not in ("failed",):
            return False

        retry_at = record.get("retry_at")
        if not retry_at:
            return False
        try:
            if datetime.fromisoformat(retry_at) > now:
                return False
        except (ValueError, TypeError):
            return False

        if record.get("attempt", 1) >= record.get("max_retries", 5):
            return False

        # Check lease — don't retry if another sweep is already executing
        locked = record.get("locked_until")
        if locked:
            try:
                if datetime.fromisoformat(locked) > now:
                    return False
            except (ValueError, TypeError):
                pass

        # Don't retry operations older than max age
        created = record.get("created_at")
        if created:
            try:
                age = now - datetime.fromisoformat(created)
                if age > timedelta(minutes=self._max_age_minutes):
                    return False
            except (ValueError, TypeError):
                pass

        return True

    def _replay(self, record: dict[str, Any]) -> dict[str, Any]:
        """Replay a capability using the registry (direct call, not MCP)."""
        try:
            from work_buddy.mcp_server.registry import get_registry, Capability

            reg = get_registry()
            entry = reg.get(record["name"])

            if entry is None:
                return {"success": False, "error": f"Capability '{record['name']}' not found in registry"}
            if not isinstance(entry, Capability):
                return {"success": False, "error": f"'{record['name']}' is a workflow, not a capability"}

            result = entry.callable(**record.get("params", {}))

            # Check for soft errors in the result
            from work_buddy.errors import is_transient_result
            if isinstance(result, dict):
                err = result.get("error")
                if err:
                    if is_transient_result(result):
                        return {"success": False, "error": str(err), "transient": True}
                    else:
                        return {"success": False, "error": str(err), "transient": False}

            # Success — update the operation record
            record["status"] = "completed"
            record["result"] = result
            record["error"] = None
            record["completed_at"] = datetime.now(timezone.utc).isoformat()
            record["locked_until"] = None
            record["queued_for_retry"] = False
            _write_record(record)

            return {"success": True, "result": result}

        except Exception as exc:
            error_str = f"{type(exc).__name__}: {exc}"

            # Classify to decide if we should keep retrying
            from work_buddy.errors import classify_error
            error_class = classify_error(exc)

            record["status"] = "failed"
            record["error"] = error_str
            record["locked_until"] = None
            _write_record(record)

            return {
                "success": False,
                "error": error_str,
                "transient": error_class == "transient",
            }

    def _on_success(self, record: dict[str, Any], replay_result: dict[str, Any]) -> None:
        """Notify the originating agent session that the retry succeeded."""
        session_id = record.get("originating_session_id")
        result_preview = str(replay_result.get("result", ""))[:500]

        # 1. Notify the agent session via messaging (if messaging is up)
        if session_id:
            try:
                from work_buddy.messaging.client import send_message, is_service_running
                if is_service_running():
                    send_message(
                        sender="sidecar:retry_queue",
                        recipient="work-buddy",
                        recipient_session=session_id,
                        type="retry_success",
                        subject=f"Retry succeeded: {record['name']}",
                        body=json.dumps({
                            "operation_id": record["operation_id"],
                            "capability": record["name"],
                            "attempt": record.get("attempt", 1),
                            "result_preview": result_preview,
                        }),
                        priority="normal",
                        tags=["retry", "success"],
                    )
            except Exception as exc:
                logger.warning("Failed to notify agent session: %s", exc)

        # 2. If part of a workflow, resume the DAG
        wf_ctx = record.get("workflow_context")
        if wf_ctx:
            self._resume_workflow(wf_ctx, replay_result.get("result"))

    def _on_exhausted(self, record: dict[str, Any], replay_result: dict[str, Any]) -> None:
        """All retries exhausted — notify the user directly."""
        # Mark as permanently failed
        record["queued_for_retry"] = False
        record["status"] = "failed"
        record["locked_until"] = None
        _write_record(record)

        # 1. Notify the agent session
        session_id = record.get("originating_session_id")
        if session_id:
            try:
                from work_buddy.messaging.client import send_message, is_service_running
                if is_service_running():
                    send_message(
                        sender="sidecar:retry_queue",
                        recipient="work-buddy",
                        recipient_session=session_id,
                        type="retry_exhausted",
                        subject=f"Retry exhausted: {record['name']}",
                        body=json.dumps({
                            "operation_id": record["operation_id"],
                            "capability": record["name"],
                            "attempts": record.get("attempt", 1),
                            "last_error": replay_result.get("error", ""),
                            "retry_history": record.get("retry_history", []),
                        }),
                        priority="high",
                        tags=["retry", "exhausted"],
                    )
            except Exception as exc:
                logger.warning("Failed to notify agent session: %s", exc)

        # 2. Notify the user via notification surfaces
        try:
            from work_buddy.notifications.models import Notification
            from work_buddy.notifications.store import create_notification
            from work_buddy.notifications.dispatcher import SurfaceDispatcher
            from work_buddy.notifications.store import mark_delivered

            notif = Notification(
                title=f"Retry exhausted: {record['name']}",
                body=(
                    f"Operation `{record['name']}` failed after "
                    f"{record.get('attempt', 1)} attempts.\n\n"
                    f"Last error: {replay_result.get('error', 'unknown')}\n\n"
                    f"Operation ID: {record['operation_id']}"
                ),
                source="sidecar:retry_queue",
                source_type="programmatic",
                priority="high",
                response_type="none",
                tags=["retry", "exhausted"],
            )
            notif = create_notification(notif)
            dispatcher = SurfaceDispatcher.from_config()
            dispatcher.deliver(notif, mark_delivered_fn=mark_delivered)
        except Exception as exc:
            logger.warning("Failed to send user notification: %s", exc)

        # 3. If part of a workflow, fail the step
        wf_ctx = record.get("workflow_context")
        if wf_ctx:
            self._fail_workflow_step(
                wf_ctx,
                f"Retry exhausted after {record.get('attempt', 1)} attempts: "
                f"{replay_result.get('error', '')}",
            )

    def _schedule_next(self, record: dict[str, Any], error: str) -> None:
        """Calculate next retry time and update the record."""
        from work_buddy.errors import compute_retry_delay

        attempt = record.get("attempt", 1)
        strategy = record.get("backoff_strategy", "adaptive")

        # If the error is no longer transient (e.g., the service returned
        # a different kind of error), stop retrying
        if not record.get("_skip_transient_check"):
            from work_buddy.errors import classify_error
            # We have the error string, not the exception — check patterns
            from work_buddy.errors import is_transient_result
            if not is_transient_result({"error": error}):
                # Not obviously transient — still retry but note it
                logger.info(
                    "Retry %s attempt %d: error may not be transient: %s",
                    record["operation_id"], attempt, error[:100],
                )

        delay = compute_retry_delay(attempt, strategy)
        now = datetime.now(timezone.utc)

        record["status"] = "failed"
        record["retry_at"] = (now + timedelta(seconds=delay)).isoformat()
        record["locked_until"] = None
        record["error"] = error

        history = record.get("retry_history", [])
        history.append({
            "attempt": attempt,
            "error": error,
            "timestamp": now.isoformat(),
        })
        record["retry_history"] = history
        _write_record(record)

    def _resume_workflow(self, wf_ctx: dict[str, Any], result: Any) -> None:
        """Resume a workflow DAG step after retry success."""
        try:
            from work_buddy.mcp_server.conductor import resume_after_retry
            resume_result = resume_after_retry(
                wf_ctx.get("workflow_run_id", ""),
                wf_ctx.get("step_id", ""),
                result,
            )
            if "error" in (resume_result or {}):
                logger.warning(
                    "Failed to resume workflow %s step %s: %s",
                    wf_ctx.get("workflow_run_id"),
                    wf_ctx.get("step_id"),
                    resume_result.get("error"),
                )
        except Exception as exc:
            logger.warning("Failed to resume workflow: %s", exc)

    def _fail_workflow_step(self, wf_ctx: dict[str, Any], error: str) -> None:
        """Fail a workflow step after retry exhaustion."""
        try:
            from work_buddy.mcp_server.conductor import fail_after_retry_exhaustion
            fail_after_retry_exhaustion(
                wf_ctx.get("workflow_run_id", ""),
                wf_ctx.get("step_id", ""),
                error,
            )
        except Exception as exc:
            logger.warning("Failed to fail workflow step: %s", exc)


# ---------------------------------------------------------------------------
# File I/O helpers (shared with gateway.py's operation record system)
# ---------------------------------------------------------------------------

def _get_operations_dir() -> Path:
    """Return the global operations directory."""
    from work_buddy.paths import data_dir
    d = data_dir("agents") / "operations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_record(record: dict[str, Any]) -> None:
    """Write an operation record back to disk (atomic)."""
    path = _get_operations_dir() / f"{record['operation_id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, default=_json_default, indent=2), encoding="utf-8")
    tmp.replace(path)


def _json_default(obj: Any) -> Any:
    """JSON serializer for dates and paths."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, Path):
        return obj.as_posix()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)
