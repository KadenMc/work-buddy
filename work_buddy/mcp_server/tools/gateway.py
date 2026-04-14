"""The gateway tools: wb_init, wb_search, wb_run, wb_advance, wb_status, wb_retry.

These are registered on the FastMCP server instance by ``register_tools()``.
All blocking calls are wrapped in ``asyncio.to_thread()`` to avoid stalling
the MCP event loop.

IMPORTANT — import discipline: The MCP server process must NEVER import
heavy compute libraries (numpy, rank_bm25, sentence-transformers, sqlite3)
because ``asyncio.to_thread`` + deferred imports = import-lock deadlock.
All heavy work goes through the embedding service HTTP API (localhost:5124).
See ``embedding/client.py`` for the HTTP client functions.
"""

from __future__ import annotations

import asyncio
import json
import os
import time as _time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from work_buddy.consent import ConsentRequired
from work_buddy.tools import ToolUnavailable
from work_buddy.mcp_server import conductor, registry

# ---------------------------------------------------------------------------
# Session registry — maps MCP session → agent session ID
# ---------------------------------------------------------------------------
# Each Claude Code session gets its own MCP protocol session (via the
# Mcp-Session-Id header on streamable-http transport).  When an agent calls
# wb_init(session_id), we store the mapping so all subsequent tool calls
# from that MCP session can be routed to the correct agent session directory.

_SESSION_REGISTRY: dict[int, str] = {}  # id(ctx.session) → agent_session_id


def _register_session(ctx: Context, agent_session_id: str) -> None:
    """Map this MCP connection to an agent session ID."""
    _SESSION_REGISTRY[id(ctx.session)] = agent_session_id


def _resolve_session(ctx: Context) -> str | None:
    """Look up the agent session ID for this MCP connection."""
    return _SESSION_REGISTRY.get(id(ctx.session))


def _require_init(ctx: Context) -> str | None:
    """Check if this MCP session has been initialized.

    Returns an error JSON string if not initialized, None if OK.
    """
    if _resolve_session(ctx) is not None:
        return None
    return _to_json({
        "error": "Session not initialized. Call wb_init(session_id) first.",
        "hint": (
            "Every agent session must call wb_init with its WORK_BUDDY_SESSION_ID "
            "before using any other work-buddy tools. Your SessionStart hook "
            "should have set this environment variable."
        ),
    })

# ---------------------------------------------------------------------------
# Operation log — durable records for retry / observability
# ---------------------------------------------------------------------------

_OPERATIONS_DIR: Path | None = None


def _get_operations_dir() -> Path:
    """Return (and lazily create) the global operations directory."""
    global _OPERATIONS_DIR
    if _OPERATIONS_DIR is None:
        # Use the repo-level agents/ dir (gitignored), not per-session
        from work_buddy.paths import data_dir
        _OPERATIONS_DIR = data_dir("agents") / "operations"
        _OPERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    return _OPERATIONS_DIR


def _save_operation(
    name: str,
    params: dict[str, Any],
    retry_policy: str,
    *,
    op_type: str = "capability",
    lease_seconds: int = 90,
) -> str:
    """Persist an operation record before dispatch. Returns the operation ID."""
    op_id = f"op_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    record = {
        "operation_id": op_id,
        "type": op_type,
        "name": name,
        "params": params,
        "retry_policy": retry_policy,
        "status": "running",
        "result": None,
        "error": None,
        "attempt": 1,
        "session_id": os.environ.get("WORK_BUDDY_SESSION_ID", "unknown"),
        "locked_until": (now + timedelta(seconds=lease_seconds)).isoformat(),
        "created_at": now.isoformat(),
        "completed_at": None,
    }
    path = _get_operations_dir() / f"{op_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, default=_json_default, indent=2), encoding="utf-8")
    tmp.replace(path)
    # Opportunistic cleanup
    _prune_old_operations()
    return op_id


def _result_error(result: Any) -> str | None:
    """Extract an error string from a result dict, if present.

    Capabilities that catch their own errors and return {"error": "..."}
    or {"success": False, "message": "..."} instead of raising need to be
    recorded as failures so wb_retry can replay them.
    Returns None when the result is not a failure.
    """
    if isinstance(result, dict):
        err = result.get("error")
        if err:  # non-None, non-empty
            return str(err)
        # Also detect {"success": False, "message": "..."} pattern
        if result.get("success") is False:
            msg = result.get("message", "")
            return str(msg) if msg else "Operation returned success=false"
    return None


def _complete_operation(op_id: str, *, result: Any = None, error: str | None = None) -> None:
    """Mark an operation as completed (success or failure)."""
    path = _get_operations_dir() / f"{op_id}.json"
    if not path.exists():
        return
    record = json.loads(path.read_text(encoding="utf-8"))
    record["status"] = "completed" if error is None else "failed"
    record["result"] = result
    record["error"] = error
    record["completed_at"] = datetime.now(timezone.utc).isoformat()
    record["locked_until"] = None
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, default=_json_default, indent=2), encoding="utf-8")
    tmp.replace(path)


def _update_operation(record: dict[str, Any]) -> None:
    """Write an updated operation record back to disk."""
    path = _get_operations_dir() / f"{record['operation_id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, default=_json_default, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_operation(op_id: str) -> dict[str, Any] | None:
    """Load an operation record by ID. Returns None if not found."""
    path = _get_operations_dir() / f"{op_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Auto-consent: pre-flight + fallback consent handling
# ---------------------------------------------------------------------------
# Instead of returning ConsentRequired errors for the agent to orchestrate
# manually, the gateway handles consent transparently: send one bundled
# notification, poll for the user's response, grant all operations on
# approval, and retry the callable.

_AUTO_CONSENT_TIMEOUT = 90  # seconds to wait for user response
_MAX_CONSENT_RETRIES = 2   # max sequential ConsentRequired retries per wb_run


def _check_missing_consent(operations: list[str]) -> list[str]:
    """Return operations from the list that lack a valid consent grant."""
    from work_buddy.consent import _cache
    return [op for op in operations if not _cache.is_granted(op)]


def _auto_consent_request(
    operations: list[str],
    capability_name: str,
    op_id: str,
    timeout: int = _AUTO_CONSENT_TIMEOUT,
) -> dict[str, Any]:
    """Send a bundled consent request and poll for user response.

    Creates ONE notification listing all operations, delivers to all
    surfaces via the dispatcher, polls for the user's choice, and grants
    consent for all operations on approval.

    Returns:
        Dict with "status" ("granted", "denied", "timeout") and details.
    """
    from work_buddy.consent import (
        get_consent_metadata, grant_consent_batch,
        create_consent_request, resolve_consent_request,
    )
    from work_buddy.notifications.store import (
        get_notification as _get_notif,
        mark_delivered as _mark_delivered,
    )
    from work_buddy.notifications.dispatcher import SurfaceDispatcher

    # Build rich body from consent metadata registry
    lines = ["This operation requires approval for:"]
    max_risk = "low"
    max_ttl = 5
    risk_order = {"low": 0, "moderate": 1, "high": 2}

    for op in operations:
        meta = get_consent_metadata(op)
        if meta:
            reason = meta["reason"]
            risk = meta["risk"]
            ttl = meta["default_ttl"]
            lines.append(f"- **{op}** ({risk}) — {reason}")
            if risk_order.get(risk, 0) > risk_order.get(max_risk, 0):
                max_risk = risk
            max_ttl = max(max_ttl, ttl)
        else:
            lines.append(f"- **{op}**")

    body = "\n".join(lines)

    # Auto-inject session ID for callback delivery
    callback_session_id = os.environ.get("WORK_BUDDY_SESSION_ID")

    # Create the consent request (uses notification substrate)
    record = create_consent_request(
        operation=operations[0] if len(operations) == 1 else f"bundle:{capability_name}",
        reason=body,
        risk=max_risk,
        default_ttl=max_ttl,
        requester=f"gateway:{capability_name}",
        context={"capability": capability_name, "operations": operations, "operation_id": op_id},
        callback_session_id=callback_session_id,
    )
    nid = record["notification_id"]

    # Deliver to all surfaces via dispatcher
    notif = _get_notif(nid)
    if notif:
        try:
            dispatcher = SurfaceDispatcher.from_config()
            dispatcher.deliver(notif, mark_delivered_fn=_mark_delivered)
        except Exception:
            pass  # Best-effort delivery

        # Poll for response via dispatcher
        try:
            response = dispatcher.poll_response(
                notif,
                timeout_seconds=timeout,
                interval_seconds=3,
            )
        except Exception:
            response = None

        if response is not None:
            # First-response-wins: dismiss on other surfaces
            notif_fresh = _get_notif(nid)
            if notif_fresh and notif_fresh.delivered_surfaces:
                try:
                    dispatcher.dismiss(notif_fresh)
                except Exception:
                    pass
    else:
        response = None

    if response is None:
        return {
            "status": "timeout",
            "request_id": nid,
            "operation_id": op_id,
            "message": (
                f"Consent request timed out, but still pending — "
                f"the user can approve on any surface. Once approved, retry with: "
                f"mcp__work-buddy__wb_retry(operation_id=\"{op_id}\")"
            ),
        }

    # Extract choice from StandardResponse
    from work_buddy.notifications.models import StandardResponse
    if isinstance(response, StandardResponse):
        choice = response.value
    elif isinstance(response, dict):
        choice = response.get("value", "deny")
    else:
        choice = str(response)
    # Dashboard wraps in {"phase": "generic", "value": "once"} — unwrap
    if isinstance(choice, dict) and "value" in choice:
        choice = choice["value"]

    if choice == "deny":
        try:
            resolve_consent_request(nid, approved=False)
        except ValueError:
            pass
        return {
            "status": "denied",
            "operation_id": op_id,
            "message": f"User denied consent for {capability_name}.",
        }

    # Approved — grant all operations with the chosen mode
    mode = choice  # "always", "temporary", or "once"
    ttl = max_ttl if mode == "temporary" else None
    try:
        resolve_consent_request(nid, approved=True, mode=mode, ttl_minutes=ttl)
    except ValueError:
        # Already resolved by surface handler — write grants manually
        grant_consent_batch(operations, mode=mode, ttl_minutes=ttl)

    return {
        "status": "granted",
        "mode": mode,
        "operations": operations,
        "operation_id": op_id,
    }


def _list_recent_operations(limit: int = 10) -> list[dict[str, Any]]:
    """List recent operations (compact summaries, no full params)."""
    ops_dir = _get_operations_dir()
    records = []
    for p in sorted(ops_dir.glob("op_*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        if len(records) >= limit:
            break
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            # Check for stale running ops
            status = raw.get("status", "unknown")
            if status == "running":
                locked = raw.get("locked_until")
                if locked:
                    try:
                        lock_dt = datetime.fromisoformat(locked)
                        if lock_dt < datetime.now(timezone.utc):
                            status = "stale"
                    except (ValueError, TypeError):
                        pass
            entry_dict = {
                "operation_id": raw["operation_id"],
                "name": raw.get("name"),
                "type": raw.get("type"),
                "status": status,
                "retry_policy": raw.get("retry_policy"),
                "attempt": raw.get("attempt"),
                "created_at": raw.get("created_at"),
                "completed_at": raw.get("completed_at"),
            }
            # Surface retry queue state
            if raw.get("queued_for_retry"):
                entry_dict["status"] = "queued_retry"
                entry_dict["retry_at"] = raw.get("retry_at")
                entry_dict["max_retries"] = raw.get("max_retries")
                entry_dict["error_class"] = raw.get("error_class")
            records.append(entry_dict)
        except (json.JSONDecodeError, KeyError):
            continue
    return records


def _enqueue_for_retry(
    op_id: str,
    error: str,
    error_class: str,
    *,
    delay_seconds: int | None = None,
    max_retries: int | None = None,
    backoff_strategy: str | None = None,
    originating_session_id: str | None = None,
    workflow_context: dict[str, Any] | None = None,
) -> None:
    """Mark a failed operation for background retry by the sidecar.

    Sets ``queued_for_retry=True`` and ``retry_at`` to ``now + delay``.
    The sidecar's retry sweep will pick this up on its next tick.

    If delay/max_retries/backoff are not provided, defaults come from
    config.yaml ``sidecar.retry_queue`` (or hardcoded fallbacks).
    """
    # Load config defaults (lightweight — cached after first load)
    try:
        from work_buddy.config import load_config
        rq_cfg = load_config().get("sidecar", {}).get("retry_queue", {})
    except Exception:
        rq_cfg = {}

    if max_retries is None:
        max_retries = rq_cfg.get("max_retries", 5)
    if backoff_strategy is None:
        backoff_strategy = rq_cfg.get("default_backoff", "adaptive")
    if delay_seconds is None:
        from work_buddy.errors import compute_retry_delay
        delay_seconds = compute_retry_delay(1, backoff_strategy)

    record = _load_operation(op_id)
    if record is None:
        return

    now = datetime.now(timezone.utc)
    record["status"] = "failed"
    record["error"] = error
    record["completed_at"] = now.isoformat()
    record["locked_until"] = None

    # Retry queue fields
    record["queued_for_retry"] = True
    record["retry_at"] = (now + timedelta(seconds=delay_seconds)).isoformat()
    record["max_retries"] = max_retries
    record["backoff_strategy"] = backoff_strategy
    record["error_class"] = error_class
    record["originating_session_id"] = originating_session_id or record.get("session_id")
    record["workflow_context"] = workflow_context
    record.setdefault("retry_history", [])
    record["retry_history"].append({
        "attempt": record.get("attempt", 1),
        "error": error,
        "error_class": error_class,
        "timestamp": now.isoformat(),
    })

    _update_operation(record)


def _prune_old_operations() -> None:
    """Remove completed operation records older than 1 hour.

    Preserves operations that are queued for retry (they need to survive
    until the sidecar processes them or they exhaust retries).
    """
    ops_dir = _get_operations_dir()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    for p in ops_dir.glob("op_*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            # Never prune operations queued for retry
            if raw.get("queued_for_retry"):
                continue
            if raw.get("status") in ("completed", "failed"):
                completed = raw.get("completed_at")
                if completed:
                    completed_dt = datetime.fromisoformat(completed)
                    if completed_dt < cutoff:
                        p.unlink(missing_ok=True)
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            continue


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register_tools(mcp: FastMCP) -> None:
    """Register the gateway tools on the given FastMCP server."""

    @mcp.tool()
    async def wb_init(session_id: str, ctx: Context = None) -> str:
        """Initialize this connection for work-buddy. MUST be called once
        at the start of every agent session before any other wb_* tool.

        The SessionStart hook sets your WORK_BUDDY_SESSION_ID environment
        variable — pass that value here.

        Args:
            session_id: Your WORK_BUDDY_SESSION_ID (from the SessionStart hook)
        """
        if not session_id or not session_id.strip():
            return _to_json({
                "error": "session_id is required. Pass your WORK_BUDDY_SESSION_ID.",
            })
        session_id = session_id.strip()
        _register_session(ctx, session_id)

        # Ensure the agent session directory exists
        from work_buddy.mcp_server.activity_ledger import record_init
        record_init(session_id)

        return _to_json({
            "status": "initialized",
            "session_id": session_id,
            "message": "Session registered. All work-buddy tools are now available.",
        })

    @mcp.tool()
    async def wb_search(query: str, category: str | None = None, filter_n: int = 3, ctx: Context = None) -> str:
        """Dynamic tool discovery for work-buddy capabilities and workflows.

        Returns full details for each match including name, description,
        type, category, and parameter schemas (with types, descriptions,
        and required flags). Use this to discover what's available and
        learn the correct parameters before calling wb_run.

        Uses hybrid BM25 + semantic search — natural language queries work
        (e.g., "what are my tasks" finds task capabilities).

        Args:
            query: Keyword to search by (matches name and description)
            category: Optional filter — messaging, contracts, status, journal, tasks, memory, context, or workflow
            filter_n: Maximum results to return (default 3)
        """
        gate = _require_init(ctx)
        if gate:
            return gate
        results = registry.search_registry(query, category, top_n=filter_n)
        # Activity ledger: record search
        from work_buddy.mcp_server.activity_ledger import record_search
        record_search(query, category, len(results), _resolve_session(ctx))
        return _to_json(results)

    @mcp.tool()
    async def wb_run(capability: str, params: str | dict | None = None, ctx: Context = None) -> str:
        """Execute a function or start a workflow.

        For functions: executes immediately and returns the result.
        For workflows: creates a DAG, starts the first step, and returns
        step instructions with a workflow_run_id for use with wb_advance.

        IMPORTANT: If you're unsure what parameters a capability accepts,
        call wb_search first — it returns full parameter schemas with types,
        descriptions, and required flags.

        Args:
            capability: Name of the capability or workflow (from wb_search)
            params: Parameters as a JSON string or dict (e.g. '{"same_day": true}' or {"same_day": true})
        """
        parsed_params = _parse_params(params)

        # Handle wb_init through wb_run (exempt from gate) — this allows
        # sessions that haven't discovered the wb_init tool to still init.
        if capability == "wb_init":
            sid = parsed_params.get("session_id", "")
            if not sid or not str(sid).strip():
                return _to_json({"error": "session_id is required. Pass your WORK_BUDDY_SESSION_ID."})
            sid = str(sid).strip()
            _register_session(ctx, sid)
            from work_buddy.mcp_server.activity_ledger import record_init
            record_init(sid)
            return _to_json({
                "status": "initialized",
                "session_id": sid,
                "message": "Session registered. All work-buddy tools are now available.",
            })

        gate = _require_init(ctx)
        if gate:
            return gate
        _agent_sid = _resolve_session(ctx)
        entry = registry.get_entry(capability)

        if entry is None:
            return _to_json({"error": f"Unknown capability: {capability!r}. Use wb_search to find available capabilities."})

        # Determine operation type and retry policy
        if isinstance(entry, registry.WorkflowDefinition):
            op_type = "workflow"
            retry_policy = "manual"
        else:
            op_type = "capability"
            retry_policy = entry.retry_policy if entry.mutates_state else "replay"

        # Save operation record before dispatch
        op_id = _save_operation(capability, parsed_params, retry_policy, op_type=op_type)

        if isinstance(entry, registry.WorkflowDefinition):
            _t0 = _time.monotonic()
            try:
                result = await asyncio.to_thread(
                    conductor.start_workflow, capability, parsed_params,
                )
            except Exception as exc:
                _complete_operation(op_id, error=f"{type(exc).__name__}: {exc}")
                return _to_json({"error": f"Workflow start failed: {exc}", "operation_id": op_id})
            _complete_operation(
                op_id, result=result,
                error=_result_error(result),
            )
            # Activity ledger: record workflow start
            from work_buddy.mcp_server.activity_ledger import record_workflow_started
            record_workflow_started(
                capability,
                result.get("workflow_run_id"),
                op_id,
                len(entry.steps),
                entry.steps[0].id if entry.steps else None,
                agent_session_id=_agent_sid,
            )
            result["operation_id"] = op_id
            return _to_json(result)

        # It's a Capability — remap aliases and validate params, then call
        if parsed_params and entry.param_aliases:
            for alias, canonical in entry.param_aliases.items():
                if alias in parsed_params and canonical not in parsed_params:
                    parsed_params[canonical] = parsed_params.pop(alias)
        if parsed_params and entry.parameters:
            known = set(entry.parameters)
            unknown = set(parsed_params) - known
            if unknown:
                param_help = registry._entry_to_dict(entry).get("parameters", {})
                msg = (
                    f"Unknown parameter(s): {', '.join(sorted(unknown))}. "
                    f"Accepted: {', '.join(sorted(known))}."
                )
                _complete_operation(op_id, error=f"Parameter error: {msg}")
                return _to_json({
                    "error": f"Parameter error: {msg}",
                    "help": f"Use wb_search('{capability}') to see accepted parameters.",
                    "parameters": param_help,
                    "operation_id": op_id,
                })

        # Inject caller's session ID for capabilities that need it
        if capability in (
            "session_activity", "session_summary", "session_wb_activity",
            "artifact_save",
        ) and _agent_sid:
            parsed_params.setdefault("agent_session_id", _agent_sid)

        from work_buddy.mcp_server.activity_ledger import record_capability
        _t0 = _time.monotonic()
        _ledger_kw = {"agent_session_id": _agent_sid}

        # --- Pre-flight consent check ---
        # If the capability declares consent_operations, check upfront
        # and request all missing consents in a single bundled notification.
        if entry.consent_operations:
            missing = await asyncio.to_thread(
                _check_missing_consent, entry.consent_operations,
            )
            if missing:
                consent_result = await asyncio.to_thread(
                    _auto_consent_request, missing, capability, op_id,
                )
                if consent_result["status"] != "granted":
                    _complete_operation(
                        op_id, error=f"Consent {consent_result['status']}: {capability}",
                    )
                    record_capability(
                        capability, entry.category, op_id, parsed_params,
                        entry.mutates_state, _t0, None,
                        f"Consent {consent_result['status']}", True,
                        ",".join(missing), **_ledger_kw,
                    )
                    consent_result["operation_id"] = op_id
                    return _to_json(consent_result)

        # --- Execute the callable (with fallback retry on ConsentRequired) ---
        _consent_retries = 0
        while True:
            try:
                result = await asyncio.to_thread(entry.callable, **parsed_params)
                break  # Success — exit retry loop
            except TypeError as exc:
                param_help = registry._entry_to_dict(entry).get("parameters", {})
                _complete_operation(op_id, error=f"Parameter error: {exc}")
                record_capability(capability, entry.category, op_id, parsed_params,
                                  entry.mutates_state, _t0, None,
                                  f"Parameter error: {exc}", False, **_ledger_kw)
                return _to_json({
                    "error": f"Parameter error: {exc}",
                    "help": f"Use wb_search('{capability}') to see accepted parameters.",
                    "parameters": param_help,
                    "operation_id": op_id,
                })
            except ConsentRequired as exc:
                _consent_retries += 1
                if _consent_retries > _MAX_CONSENT_RETRIES:
                    # Too many sequential consent gates — give up
                    _complete_operation(op_id, error=f"ConsentRequired: {exc.operation} (max retries)")
                    record_capability(capability, entry.category, op_id, parsed_params,
                                      entry.mutates_state, _t0, None,
                                      f"ConsentRequired: {exc.operation}", True, exc.operation,
                                      **_ledger_kw)
                    return _to_json({
                        "error": f"Too many consent gates for {capability}. Last: {exc.operation}",
                        "operation_id": op_id,
                    })
                # Auto-request consent for this unanticipated gate
                consent_result = await asyncio.to_thread(
                    _auto_consent_request, [exc.operation], capability, op_id,
                )
                if consent_result["status"] != "granted":
                    _complete_operation(
                        op_id, error=f"Consent {consent_result['status']}: {exc.operation}",
                    )
                    record_capability(
                        capability, entry.category, op_id, parsed_params,
                        entry.mutates_state, _t0, None,
                        f"Consent {consent_result['status']}: {exc.operation}", True,
                        exc.operation, **_ledger_kw,
                    )
                    consent_result["operation_id"] = op_id
                    return _to_json(consent_result)
                # Consent granted — retry the callable
                continue
            except ToolUnavailable as exc:
                _complete_operation(op_id, error=f"ToolUnavailable: {exc.tool_id}")
                record_capability(capability, entry.category, op_id, parsed_params,
                                  entry.mutates_state, _t0, None,
                                  f"ToolUnavailable: {exc.tool_id}", False, **_ledger_kw)
                return _to_json({
                    "tool_unavailable": True,
                    "tool_id": exc.tool_id,
                    "tool_name": exc.display_name,
                    "reason": exc.reason,
                    "operation_id": op_id,
                    "hint": (
                        f"The '{exc.display_name}' integration is not available. "
                        f"Run wb_run('feature_status') for details."
                    ),
                })
            except Exception as exc:
                error_str = f"{type(exc).__name__}: {exc}"
                _complete_operation(op_id, error=error_str)
                record_capability(capability, entry.category, op_id, parsed_params,
                                  entry.mutates_state, _t0, None,
                                  error_str, False, **_ledger_kw)

                # --- Retry queue: enqueue transient failures ---
                from work_buddy.errors import classify_error as _classify
                error_class = _classify(exc)
                if (
                    error_class == "transient"
                    and retry_policy in ("replay", "verify_first")
                ):
                    _enqueue_for_retry(
                        op_id, error_str, error_class,
                        originating_session_id=_agent_sid,
                    )
                    return _to_json({
                        "error": f"Transient failure: {error_str}",
                        "operation_id": op_id,
                        "queued_for_retry": True,
                        "retry_hint": (
                            "This operation failed due to a transient error "
                            "and has been queued for automatic background retry. "
                            "You will be notified when it succeeds. "
                            "Move on to other work."
                        ),
                    })

                return _to_json({
                    "error": f"Execution failed: {error_str}",
                    "operation_id": op_id,
                })

        # --- Check for soft transient failures in the result ---
        result_err = _result_error(result)
        if result_err:
            from work_buddy.errors import is_transient_result as _is_transient
            if (
                _is_transient(result)
                and retry_policy in ("replay", "verify_first")
            ):
                _complete_operation(op_id, result=result, error=result_err)
                record_capability(capability, entry.category, op_id, parsed_params,
                                  entry.mutates_state, _t0, result,
                                  result_err, False, **_ledger_kw)
                _enqueue_for_retry(
                    op_id, result_err, "transient",
                    originating_session_id=_agent_sid,
                )
                return _to_json({
                    "error": f"Transient failure: {result_err}",
                    "operation_id": op_id,
                    "queued_for_retry": True,
                    "retry_hint": (
                        "This operation returned a transient error "
                        "and has been queued for automatic background retry. "
                        "You will be notified when it succeeds. "
                        "Move on to other work."
                    ),
                })

        _complete_operation(op_id, result=result, error=result_err)
        record_capability(capability, entry.category, op_id, parsed_params,
                          entry.mutates_state, _t0, result,
                          result_err, False, **_ledger_kw)
        return _to_json({
            "type": "result",
            "capability": capability,
            "result": result,
            "operation_id": op_id,
        })

    @mcp.tool()
    async def wb_advance(workflow_run_id: str, step_result: str | dict | None = None, ctx: Context = None) -> str:
        """Advance a running workflow to its next step.

        Call this after completing the current step. Pass the step's output
        as step_result (JSON string). The conductor marks the step complete,
        advances the DAG, and returns the next step's instructions.

        Args:
            workflow_run_id: The run ID returned by wb_run when starting a workflow
            step_result: JSON string of the completed step's output (optional)
        """
        gate = _require_init(ctx)
        if gate:
            return gate
        parsed_result = _parse_params(step_result)
        _t0 = _time.monotonic()
        result = await asyncio.to_thread(
            conductor.advance_workflow, workflow_run_id, parsed_result,
        )
        # Activity ledger: record workflow step
        from work_buddy.mcp_server.activity_ledger import record_workflow_step
        record_workflow_step(workflow_run_id, result, _t0,
                            agent_session_id=_resolve_session(ctx))
        return _to_json(result)

    @mcp.tool()
    async def wb_status(
        workflow_run_id: str | None = None,
        operation_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Check workflow progress or system health.

        With a workflow_run_id: returns DAG progress for that workflow.
        With an operation_id: returns full details for that operation record.
        Without either: returns system overview (messaging health, active
        contracts, running workflows, recent operations).

        Args:
            workflow_run_id: Optional workflow run ID to check specific progress
            operation_id: Optional operation ID to check specific operation status
        """
        gate = _require_init(ctx)
        if gate:
            return gate
        if operation_id:
            record = _load_operation(operation_id)
            if record is None:
                return _to_json({"error": f"Unknown operation: {operation_id!r}"})
            return _to_json(record)

        if workflow_run_id:
            result = await asyncio.to_thread(
                conductor.get_workflow_status, workflow_run_id,
            )
            return _to_json(result)

        # System overview
        overview = await asyncio.to_thread(_system_overview)
        overview["recent_operations"] = _list_recent_operations(limit=10)
        return _to_json(overview)

    @mcp.tool()
    async def wb_step_result(
        workflow_run_id: str,
        step_id: str,
        key: str | None = None,
        ctx: Context = None,
    ) -> str:
        """Retrieve the full result for a specific workflow step.

        Use this when the conductor returned a manifest (``_manifest: true``)
        instead of the full data.  The visibility system elides large
        intermediate results to keep responses small — this tool lets you
        pull specific step data on demand.

        Args:
            workflow_run_id: The wf_XXXXXXXX run ID from the workflow response
            step_id: Which step's result to retrieve
            key: Optional — retrieve only this top-level key from the result dict
        """
        gate = _require_init(ctx)
        if gate:
            return gate
        result = await asyncio.to_thread(
            conductor.get_step_result, workflow_run_id, step_id, key,
        )
        return _to_json(result)

    @mcp.tool()
    async def wb_retry(operation_id: str, ctx: Context = None) -> str:
        """Retry a previously recorded operation by its ID.

        Use wb_status() to discover recent/pending operations after a timeout.
        Operations with retry_policy="manual" cannot be auto-retried.
        Operations with an active execution lease will be refused to prevent
        double-dispatch.

        Args:
            operation_id: The operation ID from wb_run response or wb_status output
        """
        gate = _require_init(ctx)
        if gate:
            return gate
        record = _load_operation(operation_id)
        if record is None:
            return _to_json({"error": f"Unknown operation: {operation_id!r}"})

        if record["retry_policy"] == "manual":
            return _to_json({
                "error": "This operation requires manual retry. Params are preserved in the record.",
                "operation": record,
            })

        if record["status"] == "completed" and not record.get("error"):
            return _to_json({
                "already_completed": True,
                "result": record["result"],
                "operation_id": operation_id,
            })

        # Check execution lease — prevent double-dispatch
        locked = record.get("locked_until")
        if locked:
            try:
                lock_dt = datetime.fromisoformat(locked)
                if lock_dt > datetime.now(timezone.utc):
                    return _to_json({
                        "status": "still_running",
                        "locked_until": locked,
                        "hint": "The previous attempt may still be executing. Wait or check wb_status.",
                    })
            except (ValueError, TypeError):
                pass

        # Replay the operation
        record["attempt"] = record.get("attempt", 1) + 1
        record["status"] = "running"
        record["locked_until"] = (
            datetime.now(timezone.utc) + timedelta(seconds=90)
        ).isoformat()
        record["error"] = None
        _update_operation(record)

        entry = registry.get_entry(record["name"])
        if entry is None:
            _complete_operation(operation_id, error=f"Capability {record['name']!r} no longer exists")
            return _to_json({"error": f"Capability {record['name']!r} no longer exists"})

        # Pre-flight consent for capabilities with declared operations
        cap_name = record["name"]
        if record["type"] != "workflow" and isinstance(entry, registry.Capability) and entry.consent_operations:
            missing = await asyncio.to_thread(
                _check_missing_consent, entry.consent_operations,
            )
            if missing:
                consent_result = await asyncio.to_thread(
                    _auto_consent_request, missing, cap_name, operation_id,
                )
                if consent_result["status"] != "granted":
                    _complete_operation(
                        operation_id,
                        error=f"Consent {consent_result['status']}: {cap_name}",
                    )
                    consent_result["operation_id"] = operation_id
                    return _to_json(consent_result)

        _consent_retries = 0
        while True:
            try:
                if record["type"] == "workflow":
                    result = await asyncio.to_thread(
                        conductor.start_workflow, record["name"], record["params"],
                    )
                else:
                    result = await asyncio.to_thread(entry.callable, **record["params"])
                break
            except ConsentRequired as exc:
                _consent_retries += 1
                if _consent_retries > _MAX_CONSENT_RETRIES:
                    _complete_operation(operation_id, error=f"ConsentRequired: {exc.operation} (max retries)")
                    return _to_json({
                        "error": f"Too many consent gates for {cap_name}. Last: {exc.operation}",
                        "operation_id": operation_id,
                    })
                consent_result = await asyncio.to_thread(
                    _auto_consent_request, [exc.operation], cap_name, operation_id,
                )
                if consent_result["status"] != "granted":
                    _complete_operation(
                        operation_id,
                        error=f"Consent {consent_result['status']}: {exc.operation}",
                    )
                    consent_result["operation_id"] = operation_id
                    return _to_json(consent_result)
                continue
            except ToolUnavailable as exc:
                _complete_operation(operation_id, error=f"ToolUnavailable: {exc.tool_id}")
                return _to_json({
                    "tool_unavailable": True,
                    "tool_id": exc.tool_id,
                    "tool_name": exc.display_name,
                    "reason": exc.reason,
                    "operation_id": operation_id,
                })
            except Exception as exc:
                error_str = f"{type(exc).__name__}: {exc}"
                _complete_operation(operation_id, error=error_str)

                # Enqueue transient failures for sidecar retry
                retry_policy = record.get("retry_policy", "replay")
                from work_buddy.errors import classify_error as _classify
                error_class = _classify(exc)
                if (
                    error_class == "transient"
                    and retry_policy in ("replay", "verify_first")
                ):
                    _enqueue_for_retry(
                        operation_id, error_str, error_class,
                        originating_session_id=record.get("originating_session_id")
                            or record.get("session_id"),
                    )
                    return _to_json({
                        "error": f"Transient failure: {error_str}",
                        "operation_id": operation_id,
                        "queued_for_retry": True,
                        "retry_hint": (
                            "Retry failed due to a transient error "
                            "and has been re-queued for automatic background retry."
                        ),
                    })

                return _to_json({
                    "error": f"Retry failed: {error_str}",
                    "operation_id": operation_id,
                })

        # Check for soft transient failures in the result
        result_err = _result_error(result)
        if result_err:
            retry_policy = record.get("retry_policy", "replay")
            from work_buddy.errors import is_transient_result as _is_transient
            if (
                _is_transient(result)
                and retry_policy in ("replay", "verify_first")
            ):
                _complete_operation(operation_id, result=result, error=result_err)
                _enqueue_for_retry(
                    operation_id, result_err, "transient",
                    originating_session_id=record.get("originating_session_id")
                        or record.get("session_id"),
                )
                return _to_json({
                    "error": f"Transient failure: {result_err}",
                    "operation_id": operation_id,
                    "queued_for_retry": True,
                    "attempt": record["attempt"],
                    "retry_hint": (
                        "Retry returned a transient error "
                        "and has been re-queued for automatic background retry."
                    ),
                })

        _complete_operation(operation_id, result=result, error=result_err)
        return _to_json({
            "type": "result",
            "capability": record["name"],
            "result": result,
            "operation_id": operation_id,
            "attempt": record["attempt"],
        })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_params(params: str | dict | None) -> dict[str, Any]:
    """Parse params — accepts JSON string, dict, or None."""
    if not params:
        return {}
    if isinstance(params, dict):
        return params
    try:
        parsed = json.loads(params)
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    except json.JSONDecodeError:
        return {"value": params}


def _to_json(obj: Any) -> str:
    """Serialize to JSON with custom handling for Path/date objects."""
    return json.dumps(obj, default=_json_default, indent=2)


def _json_default(obj: Any) -> Any:
    """JSON serializer for objects not handled by default."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return obj.as_posix()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def _system_overview() -> dict[str, Any]:
    """Build a system health overview."""
    from work_buddy.messaging.client import is_service_running

    overview: dict[str, Any] = {
        "messaging_service": "running" if is_service_running() else "offline — notify the user so they can start it",
    }

    # Embedding service
    try:
        from work_buddy.embedding.client import is_available as embed_available
        overview["embedding_service"] = "running" if embed_available() else "offline (optional — search uses BM25 without it)"
    except Exception:
        overview["embedding_service"] = "offline (optional — search uses BM25 without it)"

    # Contract summary
    try:
        from work_buddy.contracts import active_contracts, overdue_contracts
        active = active_contracts()
        overdue = overdue_contracts()
        overview["contracts"] = {
            "active": len(active),
            "overdue": len(overdue),
        }
    except Exception:
        overview["contracts"] = {"error": "Could not load contracts"}

    # Active workflow runs
    overview["active_workflows"] = conductor.list_active_runs()

    # Retry queue summary
    try:
        overview["retry_queue"] = _retry_queue_summary()
    except Exception:
        overview["retry_queue"] = {"error": "Could not read retry queue"}

    return overview


def _retry_queue_summary() -> dict[str, Any]:
    """Summarize the retry queue state."""
    ops_dir = _get_operations_dir()
    queued = 0
    exhausted = 0
    oldest_retry: str | None = None

    for p in ops_dir.glob("op_*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if raw.get("queued_for_retry"):
                if raw.get("attempt", 1) >= raw.get("max_retries", 5):
                    exhausted += 1
                else:
                    queued += 1
                    retry_at = raw.get("retry_at")
                    if retry_at and (oldest_retry is None or retry_at < oldest_retry):
                        oldest_retry = retry_at
        except (json.JSONDecodeError, OSError):
            continue

    result: dict[str, Any] = {"queued": queued}
    if exhausted:
        result["exhausted"] = exhausted
    if oldest_retry:
        result["next_retry_at"] = oldest_retry
    return result
