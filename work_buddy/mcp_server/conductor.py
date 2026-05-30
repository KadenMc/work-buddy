"""Workflow conductor — guides agents step-by-step through workflows.

The conductor wraps WorkflowDAG to provide a simple start/advance/status
interface. When an agent starts a workflow, the conductor creates a DAG
from the workflow's frontmatter-defined steps (including workflow_file
refs, execution policy, and dependencies). The agent executes each step,
calls advance() with the result, and gets the next step — the DAG
enforces ordering.

Auto-run steps
--------------
Steps with ``auto_run`` metadata are executed by the conductor in an
**isolated subprocess** — the agent never sees them as "current." The
conductor spawns ``python -m work_buddy.mcp_server.subprocess_runner``,
passes the callable path and kwargs via JSON on stdin, and reads the
result from stdout. This keeps CPU-bound steps from blocking the
shared MCP server. Chains of consecutive auto_run steps are handled
in a loop. The agent receives the auto-run outputs in ``step_results``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.mcp_server._response_audit import (
    DEFAULT_MIN_CONTAINED_KEYS,
    DEFAULT_MIN_SUBTREE,
    find_step_result_accumulations,
)
from work_buddy.mcp_server.registry import (
    ResultVisibility, WorkflowDefinition, WorkflowStep, get_entry,
)
from work_buddy.workflow import TaskStatus, WorkflowDAG

logger = logging.getLogger(__name__)


# In-memory map of active workflow runs.
# Key: workflow_run_id, Value: WorkflowDAG instance
_ACTIVE_RUNS: dict[str, WorkflowDAG] = {}

# Guards compound mutations of _ACTIVE_RUNS. The dict is touched both by
# gateway request workers (start/advance run on asyncio.to_thread threads)
# and by the idle-sweep daemon thread. Single dict ops are GIL-atomic, but
# this lock makes snapshot-then-iterate and check-then-delete sequences
# safe. It is held only for microsecond dict ops — never across _save()
# (disk I/O) or subprocess calls — so it cannot serialize real work.
_ACTIVE_RUNS_LOCK = threading.Lock()

# Fallback idle timeout when config is unavailable. The config surface is
# workflows.run_lifecycle.idle_timeout_hours (see config.yaml).
_DEFAULT_IDLE_TIMEOUT_HOURS = 24.0

# TTL ceiling for a headless (sidecar-scheduled) run's workflow_run grant.
# Normal completion revokes it early; this bounds how long an abnormally-
# terminated headless run's grant can linger before lazy-expiry reaps it.
# Generous relative to any real scheduled job, tight relative to "forever".
_SIDECAR_RUN_GRANT_TTL_MINUTES = 6 * 60


def _validate_workflow_params(
    entry: WorkflowDefinition,
    params: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    """Check caller-provided params against the workflow's declared schema.

    Strict policy: a workflow without ``params_schema`` rejects any
    non-empty params; a workflow with a schema rejects unknown keys and
    requires all keys marked ``required: true`` to be present.

    Returns ``(ok, error_message)``. On success, ``error_message`` is None.
    """
    schema = entry.params_schema or {}
    params = params or {}

    if not schema:
        if params:
            return False, (
                f"Workflow {entry.name!r} declares no params_schema but received "
                f"params: {sorted(params.keys())}. Declare a schema on the workflow "
                f"to accept caller params."
            )
        return True, None

    unknown = sorted(set(params) - set(schema))
    if unknown:
        return False, (
            f"Unknown param(s) {unknown} for workflow {entry.name!r}; "
            f"declared params: {sorted(schema)}."
        )

    missing = sorted(
        name for name, spec in schema.items()
        if isinstance(spec, dict) and spec.get("required") and name not in params
    )
    if missing:
        return False, (
            f"Missing required param(s) {missing} for workflow {entry.name!r}."
        )

    return True, None


def _resolve_params_source(
    source: str,
    initial_params: dict[str, Any] | None,
) -> tuple[bool, Any]:
    """Resolve a ``__params__`` (optionally dotted) source from initial params.

    ``__params__`` returns the whole dict; ``__params__.foo`` walks one
    level; ``__params__.a.b`` walks deeper. Returns ``(found, value)``.
    Missing keys at any depth → ``(False, None)``.
    """
    initial_params = initial_params or {}
    if source == "__params__":
        return True, initial_params
    if not source.startswith("__params__."):
        return False, None
    cursor: Any = initial_params
    for part in source.split(".")[1:]:
        if not isinstance(cursor, dict) or part not in cursor:
            return False, None
        cursor = cursor[part]
    return True, cursor


def start_workflow(
    workflow_name: str,
    params: dict[str, Any] | None = None,
    agent_session_id: str | None = None,
    *,
    headless: bool = False,
) -> dict[str, Any]:
    """Start a workflow and return its first available step.

    Returns a dict with:
      - workflow_run_id
      - workflow_context (philosophy, "What NOT to do" — first step only)
      - current_step (with instruction, workflow_file if applicable)
      - diagram (Mermaid flowchart)

    ``headless=True`` marks a run with no interactive agent behind it (a
    sidecar-scheduled cron job). When set and no ``agent_session_id`` was
    given, the run is minted under a dedicated, isolated per-run session
    (``sidecar-run-<run_id>``) so its ``workflow_run`` grant cannot carry-
    authorize any operation outside this run (e.g. a concurrent sidecar
    ``agent_spawn``), and the grant is given a finite TTL ceiling so an
    abnormally-terminated run self-heals instead of orphaning forever.
    """
    entry = get_entry(workflow_name)
    if entry is None:
        return {"error": f"Unknown workflow: {workflow_name!r}"}
    if not isinstance(entry, WorkflowDefinition):
        return {"error": f"{workflow_name!r} is a function, not a workflow. Use wb_run to execute it."}
    if not entry.steps:
        return {"error": f"Workflow {workflow_name!r} has no steps defined in frontmatter."}

    ok, err = _validate_workflow_params(entry, params)
    if not ok:
        return {"error": err}

    run_id = f"wf_{uuid.uuid4().hex[:8]}"

    # Headless (sidecar-scheduled) runs with no caller session get a
    # dedicated, isolated session so their run grant is quarantined from the
    # sidecar's standing-grant DB (and every agent DB). The id leads with the
    # uuid-based run_id so its 8-char short id (which get_session_dir uses to
    # key the on-disk session directory) is unique per run — a "sidecar-run-*"
    # prefix would collide on the shared short id "sidecar-" and defeat the
    # isolation.
    if headless and not agent_session_id:
        agent_session_id = f"{run_id}-srun"

    dag = WorkflowDAG(
        name=f"{workflow_name}:{run_id}",
        description=f"Run of workflow {workflow_name}",
    )
    # Stash caller-provided params on the DAG. Persisted via _save and
    # re-read by _execute_auto_run when an input_map references __params__.
    dag.initial_params = dict(params or {})  # type: ignore[attr-defined]

    # Populate the DAG from frontmatter-defined steps — including workflow_file
    for step in entry.steps:
        meta: dict[str, Any] = {
            "instruction": step.instruction,
            "step_type": step.step_type,
            "optional": step.optional,
        }
        if step.auto_run is not None:
            meta["auto_run"] = {
                "callable": step.auto_run.callable,
                "kwargs": step.auto_run.kwargs,
                "input_map": step.auto_run.input_map,
                "timeout": step.auto_run.timeout,
            }
        if step.result_schema is not None:
            meta["result_schema"] = step.result_schema
        if step.requires_individual_consent:
            meta["requires_individual_consent"] = True
        if step.requires:
            meta["requires"] = list(step.requires)

        dag.add_task(
            task_id=step.id,
            name=step.name,
            workflow_file=step.workflow_file,
            execution=step.execution,
            depends_on=step.depends_on if step.depends_on else None,
            metadata=meta,
        )

    # Pin the caller's session to the DAG *before* the first save, so the
    # persisted file — and any run recovered from it after an MCP-server
    # restart — can resolve consent against the right session. Every
    # auto_run subprocess started from this workflow also uses the agent's
    # consent.db via this pin; without it the subprocess reads
    # WORK_BUDDY_SESSION_ID from its parent (the MCP server's own session)
    # and consent grants land in a different DB.
    dag.agent_session_id = agent_session_id
    # Pin the workflow's class name so downstream lifecycle code (revoke
    # at completion, cascade from class-revoke, orphan reconciliation)
    # can look it up without parsing dag.name.
    dag.workflow_name = workflow_name  # type: ignore[attr-defined]

    dag.save()
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS[run_id] = dag

    # Composable consent: mint the run-level grant for this workflow run.
    # The class-level grant (if any) is checked/minted by the gateway's
    # pre-flight branch BEFORE this method is called — the run grant
    # alone is sufficient to authorize the workflow's sub-operations
    # via the @requires_consent carry path.
    from work_buddy.consent import grant_workflow_run
    # Headless runs get a TTL ceiling on the run grant (defense in depth):
    # normal completion still revokes early, but an abnormally-terminated
    # headless run self-heals at expiry instead of lingering forever.
    grant_workflow_run(
        workflow_name, run_id, session_id=agent_session_id,
        ttl_minutes=_SIDECAR_RUN_GRANT_TTL_MINUTES if headless else None,
    )

    # Build response with workflow context on first step
    response = _build_response(run_id, dag)

    # Include workflow-level context (philosophy, constraints) on start only
    if entry.context:
        response["workflow_context"] = entry.context

    # Surface caller-provided initial params to the agent so a reasoning
    # first step can read them. Only included when the workflow accepts
    # params and the caller actually supplied some.
    if dag.initial_params:  # type: ignore[attr-defined]
        response["initial_params"] = dag.initial_params  # type: ignore[attr-defined]

    return response


def advance_workflow(
    workflow_run_id: str,
    step_result: Any | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Complete the current step and return the next one.

    The caller should pass the result of the step they just executed.
    The conductor marks the current running step as complete, advances
    the DAG, and returns the next available step along with the
    prior step's result (for data threading).
    """
    dag = _ACTIVE_RUNS.get(workflow_run_id)
    if dag is None:
        return {"error": f"Unknown workflow run: {workflow_run_id!r}. It may have been completed or lost."}

    # If the caller supplied a fresher agent session id (e.g. resumption
    # after an MCP reload re-registered the agent), keep the DAG's pinned
    # session in sync. Otherwise leave whatever was stored at start_workflow.
    if agent_session_id and getattr(dag, "agent_session_id", None) != agent_session_id:
        dag.agent_session_id = agent_session_id  # type: ignore[attr-defined]

    # Find the currently running step
    running = [
        nid for nid, data in dag._graph.nodes(data=True)
        if data.get("status") == "running"
    ]

    if not running:
        available = dag.next_available()
        if not available:
            return _build_complete_response(workflow_run_id, dag)
        task = available[0]
        dag.start_task(task["task_id"])
        return _build_response(workflow_run_id, dag)

    # Complete the running step with the agent's result
    current_id = running[0]
    serialized_result = _safe_serialize(step_result)

    # --- Validate result against schema if declared ---
    current_meta = dag._graph.nodes[current_id].get("metadata", {})
    result_schema = current_meta.get("result_schema")
    if result_schema:
        validation_error = _validate_step_result(current_id, serialized_result, result_schema)
        if validation_error:
            logger.warning(
                "Schema validation failed for step '%s': %s",
                current_id, validation_error,
            )
            # Pivot the hint on what the agent actually sent. An empty
            # dict almost always means the parameter never reached the
            # conductor (omitted or misnamed); the generic "re-read the
            # instructions and pass the full data structure" hint
            # misdirects in that case. The error message itself already
            # carries the parameter-name nudge — the hint here mirrors
            # that framing so the two surfaces tell the same story.
            if isinstance(serialized_result, dict) and not serialized_result:
                hint = (
                    "The conductor received an empty result. Verify the "
                    "call shape: `wb_advance(workflow_run_id=..., "
                    "step_result={...})`. The parameter is `step_result`, "
                    "not `result` — FastMCP silently drops unknown kwargs."
                )
            else:
                hint = (
                    "Re-read the step instructions carefully. Your step result "
                    "must be the complete data structure (e.g. the full "
                    "presentation dict), not a summary or file reference. "
                    "Call wb_advance again with the correct result."
                )
            return {
                "type": "validation_error",
                "workflow_run_id": workflow_run_id,
                "step_id": current_id,
                "error": validation_error,
                "hint": hint,
                "diagram": _dag_to_mermaid(dag),
            }

    dag.complete_task(current_id, result=serialized_result)

    # Surface cross-step accumulation if the just-completed step's result
    # contains a prior step's result as a key-by-key subset (Problem C).
    # Logged at WARN, never blocking — agents in the wild may slip into
    # the pattern and the conductor's job is to make it audible, not to
    # silently mutate the response.
    _warn_if_accumulating(dag, current_id)

    # If the completed step opted into individual consent, re-mint the
    # run-level workflow grant so subsequent (non-opted-out) steps resume
    # their carry-via-workflow behavior. Route via the DAG-pinned session.
    if current_meta.get("requires_individual_consent", False):
        from work_buddy.consent import grant_workflow_run
        wf_name = getattr(dag, "workflow_name", None) or (
            (getattr(dag, "name", "") or "").split(":", 1)[0] if ":" in (getattr(dag, "name", "") or "") else getattr(dag, "name", "")
        )
        grant_workflow_run(
            wf_name,
            workflow_run_id,
            session_id=getattr(dag, "agent_session_id", None),
        )
        logger.info(
            "Main-execution step '%s' complete — workflow run grant re-minted",
            current_id,
        )

    # Check if workflow is now complete
    if dag.is_complete():
        result = _build_complete_response(workflow_run_id, dag)
        with _ACTIVE_RUNS_LOCK:
            _ACTIVE_RUNS.pop(workflow_run_id, None)
        return result

    # Build response — auto_run steps are consumed transparently inside.
    # The auto_run chain may complete the workflow, producing a
    # ``workflow_complete`` response.  Pass ``prior_step_id`` so
    # ``_build_response`` includes the just-completed step's result in
    # ``step_results`` for continuity (no post-hoc override needed).
    response = _build_response(workflow_run_id, dag, prior_step_id=current_id)

    if response.get("type") == "workflow_complete":
        # Auto-run chain finished the workflow — clean up
        with _ACTIVE_RUNS_LOCK:
            _ACTIVE_RUNS.pop(workflow_run_id, None)
        return response

    # ``prior_step`` is a pointer; the just-completed step's result lives
    # in ``step_results[current_id]`` (kept canonical, single copy).
    response["prior_step"] = {"id": current_id}
    return response


def get_workflow_status(workflow_run_id: str) -> dict[str, Any]:
    """Get progress summary for a running workflow."""
    dag = _ACTIVE_RUNS.get(workflow_run_id)
    if dag is None:
        return {"error": f"Unknown workflow run: {workflow_run_id!r}"}

    return {
        "workflow_run_id": workflow_run_id,
        "summary": dag.summary(),
        "is_complete": dag.is_complete(),
        "diagram": _dag_to_mermaid(dag),
    }


def list_active_runs() -> list[dict[str, Any]]:
    """List all active workflow runs."""
    with _ACTIVE_RUNS_LOCK:
        snapshot = list(_ACTIVE_RUNS.items())
    return [
        {
            "workflow_run_id": run_id,
            "name": dag.name,
            "is_complete": dag.is_complete(),
            "cancelled": getattr(dag, "cancelled", False),
        }
        for run_id, dag in snapshot
    ]


def reconcile_workflow_consent(session_id: str) -> dict[str, Any]:
    """Revoke orphaned workflow-consent grants left by a server restart.

    Called at session registration. A workflow mints grants into the
    agent's ``consent.db`` (``workflow_run:<name>:<run_id>`` keys plus,
    when v1's pre-flight prompt is taken, ``workflow_class:<name>`` keys)
    and pins the run's DAG in the in-memory ``_ACTIVE_RUNS`` map. The two
    have mismatched lifetimes: an MCP-server restart wipes
    ``_ACTIVE_RUNS`` but the on-disk grants survive — they would silently
    authorize every consent-gated call until they expire (run grants are
    untimed; class grants live up to their TTL).

    This sweep re-couples them:

    - ``workflow_run:*`` keys: if no ``_ACTIVE_RUNS`` entry has a matching
      ``run_id`` for the same session, revoke. Genuinely in-flight runs
      keep their DAGs in ``_ACTIVE_RUNS`` until completion, so they pass
      the match check. Cleaned up additively — does not affect the
      legacy-blanket return-shape contract below.
    - Legacy ``__workflow_consent__``: revoke when no in-flight run
      belongs to this session. The return shape preserves the original
      contract: ``{"swept": True}`` on revoke; ``{"swept": False,
      "reason": "active_run_present"}`` when an in-flight run protects
      the blanket; ``{"swept": False, "reason": "no_blanket"}`` when
      nothing to sweep on the legacy key. Callers (and existing tests)
      depend on these exact reason strings.
    - ``workflow_class:*`` keys: left alone — their TTL is the intended
      lifetime bound, and they outlive the workflow run by design.

    Never raises — a failure here must not break session registration.
    """
    if not session_id:
        return {"swept": False, "reason": "no_session"}
    try:
        from work_buddy.consent import (
            is_workflow_consent_active, revoke_workflow_consent,
            list_active_workflow_grants, revoke_workflow_run,
        )

        active_run_ids_for_session = {
            run_id
            for run_id, dag in _snapshot_active_runs()
            if getattr(dag, "agent_session_id", None) == session_id
        }

        # ── New composable keys: orphaned ``workflow_run:*`` keys ─────
        # Additive cleanup; does not affect return-shape contract below.
        orphaned_run_keys: list[str] = []
        try:
            snapshot = list_active_workflow_grants(session_id=session_id)
            for run_entry in snapshot.get("run", []):
                wf_name = run_entry.get("workflow_name", "")
                run_id = run_entry.get("run_id", "")
                if run_id and run_id in active_run_ids_for_session:
                    continue
                revoke_workflow_run(
                    wf_name, run_id,
                    session_id=session_id,
                    reason="orphan_reconcile",
                )
                orphaned_run_keys.append(f"{wf_name}:{run_id}")
                logger.info(
                    "Revoked orphaned workflow_run grant %s for session "
                    "%s (no active run — likely an MCP-server restart)",
                    f"{wf_name}:{run_id}", session_id[:8],
                )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "reconcile_workflow_consent: workflow_run sweep failed "
                "for session %s: %s",
                session_id[:8] if session_id else session_id, exc,
            )

        # ── Legacy ``__workflow_consent__`` blanket ────────────────────
        # The return-shape contract below matches what existing callers
        # and tests expect — do not change without updating them.
        if not is_workflow_consent_active(session_id=session_id):
            result: dict[str, Any] = {"swept": False, "reason": "no_blanket"}
            if orphaned_run_keys:
                result["orphaned_run_keys"] = orphaned_run_keys
            return result
        if active_run_ids_for_session:
            result = {"swept": False, "reason": "active_run_present"}
            if orphaned_run_keys:
                result["orphaned_run_keys"] = orphaned_run_keys
            return result
        revoke_workflow_consent(session_id=session_id)
        logger.info(
            "Revoked orphaned legacy workflow blanket for session %s "
            "(no active run — likely an MCP-server restart)",
            session_id[:8],
        )
        result = {"swept": True}
        if orphaned_run_keys:
            result["orphaned_run_keys"] = orphaned_run_keys
        return result
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "reconcile_workflow_consent failed for session %s: %s",
            session_id[:8] if session_id else session_id, exc,
        )
        return {"swept": False, "reason": "error"}


def _snapshot_active_runs() -> list[tuple[str, Any]]:
    """Return a thread-safe snapshot of ``_ACTIVE_RUNS`` as a list of
    ``(run_id, dag)`` tuples. Used by reconciliation + cascade revoke.
    """
    with _ACTIVE_RUNS_LOCK:
        return list(_ACTIVE_RUNS.items())


def cascade_revoke_workflow(
    workflow_name: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Revoke a workflow's class grant AND all in-flight run grants.

    The ocap CDT model: revoking the parent (class grant) revokes all
    derived children (run grants). Used when the user explicitly
    withdraws trust for a workflow class — in-flight runs should not
    continue to authorize calls under the now-withdrawn approval.

    Returns ``{"revoked_class": bool, "revoked_runs": [run_id, ...]}``.
    Never raises — best-effort cleanup.
    """
    from work_buddy.consent import (
        revoke_workflow_class, revoke_workflow_run,
    )

    revoked_class = False
    try:
        revoke_workflow_class(workflow_name, session_id=session_id)
        revoked_class = True
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "cascade_revoke_workflow: class revoke failed for %s: %s",
            workflow_name, exc,
        )

    revoked_runs: list[str] = []
    for run_id, dag in _snapshot_active_runs():
        dag_workflow_name = getattr(dag, "workflow_name", None) or (
            (getattr(dag, "name", "") or "").split(":", 1)[0]
            if ":" in (getattr(dag, "name", "") or "")
            else getattr(dag, "name", "")
        )
        if dag_workflow_name != workflow_name:
            continue
        # Scope the cascade to the matching session when one is supplied;
        # otherwise revoke regardless (caller asked for global cleanup).
        if session_id is not None:
            if getattr(dag, "agent_session_id", None) != session_id:
                continue
        try:
            revoke_workflow_run(
                workflow_name,
                run_id,
                session_id=getattr(dag, "agent_session_id", session_id),
                reason="cascade",
            )
            revoked_runs.append(run_id)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "cascade_revoke_workflow: run revoke failed for %s:%s: %s",
                workflow_name, run_id, exc,
            )

    return {
        "workflow_name": workflow_name,
        "revoked_class": revoked_class,
        "revoked_runs": revoked_runs,
    }


def get_step_result(
    workflow_run_id: str,
    step_id: str,
    key: str | None = None,
) -> dict[str, Any]:
    """Retrieve the full result for a specific workflow step.

    Agents call this (via ``wb_step_result``) to fetch data that was
    elided by the visibility system.  Checks in-memory active runs
    first, then falls back to the DAG state file on disk.
    """
    dag = _ACTIVE_RUNS.get(workflow_run_id)

    # Fallback: search persisted DAG files for completed workflows
    if dag is None:
        dag = _load_dag_from_disk(workflow_run_id)

    if dag is None:
        return {"error": f"Workflow {workflow_run_id!r} not found (active or on disk)"}

    all_results = dag.get_all_results()
    if step_id not in all_results:
        return {
            "error": f"No result for step {step_id!r}",
            "available_steps": list(all_results.keys()),
        }

    result = all_results[step_id]

    if key is not None:
        if isinstance(result, dict) and key in result:
            value = result[key]
            # Cap individual key results too
            try:
                serialized = json.dumps(value, default=str)
            except (TypeError, ValueError):
                serialized = str(value)
            if len(serialized) > _STEP_RESULT_CAP:
                return {
                    "step_id": step_id,
                    "key": key,
                    "_truncated": True,
                    "_size": len(serialized),
                    "_message": f"Key value too large ({len(serialized):,} chars). "
                                f"Full data is in the DAG state file.",
                }
            return {"step_id": step_id, "key": key, "value": value}
        available = list(result.keys()) if isinstance(result, dict) else []
        return {"error": f"Key {key!r} not found in step {step_id!r}", "available_keys": available}

    # Return full result (subject to cap)
    try:
        serialized = json.dumps(result, default=str)
    except (TypeError, ValueError):
        serialized = str(result)
    if len(serialized) > _STEP_RESULT_CAP:
        return {
            "step_id": step_id,
            "_truncated": True,
            "_size": len(serialized),
            "_keys": list(result.keys()) if isinstance(result, dict) else None,
            "_message": f"Result too large ({len(serialized):,} chars). "
                        f"Use the 'key' parameter to retrieve specific keys.",
        }
    return {"step_id": step_id, "result": result}


def _iter_dag_files() -> Iterator[Path]:
    """Yield every persisted workflow-DAG JSON file across all agent sessions.

    A DAG is saved under the ``workflows/`` directory of whichever session
    the saving process belonged to. After an MCP-server restart there is
    no meaningful "current session", and the persistent gateway runs under
    its own synthetic session — so any code that needs to find a run by id
    (the disk fallback, restart recovery) must scan every session.

    Scans ``agents/*/workflows/`` directly rather than via ``list_sessions``
    so a session directory missing its ``manifest.json`` is still covered.
    """
    from work_buddy.agent_session import get_agents_dir

    try:
        agents_dir = get_agents_dir()
    except Exception:  # pragma: no cover — defensive
        return
    if not agents_dir.is_dir():
        return
    for session_dir in sorted(agents_dir.iterdir()):
        wf_dir = session_dir / "workflows"
        if not wf_dir.is_dir():
            continue
        for path in sorted(wf_dir.glob("*.json")):
            try:
                if path.stat().st_size == 0:
                    continue  # empty file — never a valid DAG
            except OSError:  # pragma: no cover — defensive
                continue
            yield path


def _load_dag_from_disk(workflow_run_id: str) -> WorkflowDAG | None:
    """Find a persisted DAG by run id, scanning every agent session.

    The run id is matched against the DAG ``name`` (format
    ``"<workflow>:<run_id>"``), not the filename — filenames are
    lower-cased and colon-sanitized, so the id may not survive verbatim.
    """
    for path in _iter_dag_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if workflow_run_id in data.get("name", ""):
            try:
                return WorkflowDAG.load(path)
            except Exception:  # pragma: no cover — defensive
                continue
    return None


def resume_after_retry(
    workflow_run_id: str,
    step_id: str,
    result: Any,
) -> dict[str, Any]:
    """Resume a workflow after a retry-pending step succeeds.

    Called by the sidecar's retry sweep when a queued operation
    that was part of a workflow completes successfully.

    Transitions: retry_pending → completed, then unblocks dependents.
    """
    dag = _ACTIVE_RUNS.get(workflow_run_id)
    if dag is None:
        return {"error": f"Workflow {workflow_run_id!r} not active (may have timed out or completed)"}

    node_data = dag._graph.nodes.get(step_id)
    if node_data is None:
        return {"error": f"Step {step_id!r} not found in workflow {workflow_run_id!r}"}

    if node_data["status"] != TaskStatus.RETRY_PENDING.value:
        return {
            "error": f"Step {step_id!r} is {node_data['status']!r}, not retry_pending",
            "current_status": node_data["status"],
        }

    # Transition: retry_pending → completed (via running)
    node_data["status"] = TaskStatus.RUNNING.value
    dag.complete_task(step_id, result=result)

    logger.info(
        "Workflow %s step %s resumed after retry success",
        workflow_run_id, step_id,
    )

    return {
        "resumed": True,
        "workflow_run_id": workflow_run_id,
        "step_id": step_id,
        "is_complete": dag.is_complete(),
        "next_available": [t["task_id"] for t in dag.next_available()],
    }


def fail_after_retry_exhaustion(
    workflow_run_id: str,
    step_id: str,
    error: str,
) -> dict[str, Any]:
    """Permanently fail a workflow step after all retries are exhausted.

    Called by the sidecar's retry sweep when a queued operation
    exhausts its retry attempts.

    Transitions: retry_pending → failed.
    """
    dag = _ACTIVE_RUNS.get(workflow_run_id)
    if dag is None:
        return {"error": f"Workflow {workflow_run_id!r} not active"}

    node_data = dag._graph.nodes.get(step_id)
    if node_data is None:
        return {"error": f"Step {step_id!r} not found"}

    if node_data["status"] != TaskStatus.RETRY_PENDING.value:
        return {"error": f"Step {step_id!r} is {node_data['status']!r}, not retry_pending"}

    dag.fail_task(step_id, error=error)

    logger.info(
        "Workflow %s step %s failed after retry exhaustion: %s",
        workflow_run_id, step_id, error[:100],
    )

    return {
        "failed": True,
        "workflow_run_id": workflow_run_id,
        "step_id": step_id,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Run lifecycle — cancel, idle sweep, restart recovery
# ---------------------------------------------------------------------------


def _idle_threshold_hours(override: float | None = None) -> float:
    """Resolve the idle-timeout threshold in hours.

    An explicit ``override`` wins; otherwise read
    ``workflows.run_lifecycle.idle_timeout_hours`` from config, falling
    back to the module default.
    """
    if override is not None:
        return float(override)
    try:
        from work_buddy.config import load_config
        cfg = load_config().get("workflows", {}).get("run_lifecycle", {})
        return float(cfg.get("idle_timeout_hours", _DEFAULT_IDLE_TIMEOUT_HOURS))
    except Exception:  # pragma: no cover — defensive
        return _DEFAULT_IDLE_TIMEOUT_HOURS


def _parse_ts(raw: Any) -> datetime | None:
    """Parse an ISO-8601 string to an aware UTC datetime, or None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _run_last_activity(dag: WorkflowDAG) -> datetime:
    """Return the timestamp of the run's most recent step progress.

    Idleness is measured from genuine workflow progress — the freshest
    ``started_at`` / ``completed_at`` across all DAG nodes — not from the
    file's ``saved_at`` (which also advances on non-progress writes).
    Falls back to the run's ``created_at`` when no task has run yet, and
    to "now" if even that is unparseable (a malformed run is treated as
    fresh rather than swept on sight).
    """
    latest: datetime | None = None
    for _, data in dag._graph.nodes(data=True):
        for field in ("completed_at", "started_at"):
            ts = _parse_ts(data.get(field))
            if ts is not None and (latest is None or ts > latest):
                latest = ts
    if latest is None:
        latest = _parse_ts(getattr(dag, "_created_at", "")) or datetime.now(timezone.utc)
    return latest


def cancel_workflow(
    workflow_run_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Cancel a workflow run.

    Marks the on-disk DAG cancelled (kept for audit), drops the run from
    the in-memory active-runs map, and revokes the workflow consent
    blanket. Looks the run up in ``_ACTIVE_RUNS`` first; if it is not
    active (e.g. only on disk after a restart) it falls back to the
    persisted DAG so a stale run can still be cleaned.

    Idempotent: cancelling an already-cancelled run is a no-op, and a run
    that has already completed is left untouched.
    """
    reason = reason or "user_requested"

    with _ACTIVE_RUNS_LOCK:
        dag = _ACTIVE_RUNS.get(workflow_run_id)
    was_active = dag is not None

    if dag is None:
        dag = _load_dag_from_disk(workflow_run_id)

    if dag is None:
        return {
            "cancelled": False,
            "workflow_run_id": workflow_run_id,
            "error": (
                f"Unknown workflow run: {workflow_run_id!r} "
                f"(not active and not found on disk)."
            ),
        }

    if getattr(dag, "cancelled", False):
        # Already cancelled — ensure it isn't lingering in the map; no-op.
        with _ACTIVE_RUNS_LOCK:
            _ACTIVE_RUNS.pop(workflow_run_id, None)
        return {
            "cancelled": True,
            "already_cancelled": True,
            "workflow_run_id": workflow_run_id,
            "reason": getattr(dag, "cancelled_reason", None),
        }

    if dag.is_complete():
        return {
            "cancelled": False,
            "workflow_run_id": workflow_run_id,
            "detail": "Workflow run already complete — nothing to cancel.",
        }

    # Mark cancelled on disk first (the audit trail survives even if the
    # process dies right here), then drop from the active map.
    dag.mark_cancelled(reason)
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS.pop(workflow_run_id, None)

    # Revoke the workflow run grant via the DAG-pinned session, so a
    # cancelled run never leaves a live grant behind in the agent's DB.
    try:
        from work_buddy.consent import revoke_workflow_run
        wf_name = getattr(dag, "workflow_name", None) or (
            (getattr(dag, "name", "") or "").split(":", 1)[0] if ":" in (getattr(dag, "name", "") or "") else getattr(dag, "name", "")
        )
        revoke_workflow_run(
            wf_name,
            workflow_run_id,
            session_id=getattr(dag, "agent_session_id", None),
            reason="cancelled",
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "cancel_workflow: consent revoke failed for %s: %s",
            workflow_run_id, exc,
        )

    logger.info(
        "Workflow run %s cancelled (reason=%s, was_active=%s)",
        workflow_run_id, reason, was_active,
    )
    return {
        "cancelled": True,
        "workflow_run_id": workflow_run_id,
        "reason": reason,
        "was_active": was_active,
    }


def sweep_idle_runs(
    idle_threshold_hours: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Cancel active workflow runs idle past the timeout.

    An orphaned run — one whose agent stopped calling ``wb_advance`` —
    never leaves ``_ACTIVE_RUNS`` on its own; this sweep reclaims it. It
    runs in the same process as ``_ACTIVE_RUNS`` (the MCP gateway), so it
    works whether invoked by the background sweep thread or manually via
    ``wb_run``. Complete and already-cancelled runs are skipped.
    """
    threshold = _idle_threshold_hours(idle_threshold_hours)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=threshold)

    with _ACTIVE_RUNS_LOCK:
        snapshot = list(_ACTIVE_RUNS.items())

    candidates: list[dict[str, Any]] = []
    cancelled: list[str] = []

    for run_id, dag in snapshot:
        if dag.is_complete() or getattr(dag, "cancelled", False):
            continue
        last = _run_last_activity(dag)
        if last >= cutoff:
            continue
        candidates.append({
            "workflow_run_id": run_id,
            "name": dag.name,
            "idle_hours": round((now - last).total_seconds() / 3600.0, 2),
        })
        if not dry_run:
            result = cancel_workflow(run_id, reason="idle_timeout")
            if result.get("cancelled"):
                cancelled.append(run_id)

    if candidates:
        logger.info(
            "Idle-run sweep: %d candidate(s), %d cancelled "
            "(threshold=%.1fh, dry_run=%s)",
            len(candidates), len(cancelled), threshold, dry_run,
        )
    return {
        "checked": len(snapshot),
        "candidates": candidates,
        "cancelled": cancelled,
        "dry_run": dry_run,
        "threshold_hours": threshold,
    }


def recover_active_runs(
    idle_threshold_hours: float | None = None,
) -> dict[str, Any]:
    """Reload incomplete workflow runs from disk into ``_ACTIVE_RUNS``.

    The in-memory active-runs map does not survive an MCP-server restart,
    but the DAG state on disk does. Without this, a restart silently
    abandons every in-flight workflow — an agent's next ``wb_advance``
    would get "unknown run". Called once at gateway startup.

    Runs that are complete or already cancelled are not recovered. Runs
    idle past the timeout are not recovered either — they are marked
    cancelled on disk so they don't resurface on the next boot.
    """
    threshold = _idle_threshold_hours(idle_threshold_hours)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=threshold)

    recovered: list[str] = []
    expired: list[str] = []
    skipped = 0
    errors = 0

    for path in _iter_dag_files():
        try:
            dag = WorkflowDAG.load(path)
        except Exception:
            # Corrupt or unreadable file — skip it, don't fail the boot.
            errors += 1
            continue

        # run_id is the segment after the last ':' in "<workflow>:<run_id>".
        name = dag.name or ""
        if ":" not in name:
            logger.warning(
                "recover_active_runs: DAG at %s has un-keyable name %r — "
                "skipping", path, name,
            )
            skipped += 1
            continue
        run_id = name.rsplit(":", 1)[1]

        if dag.is_complete() or getattr(dag, "cancelled", False):
            skipped += 1
            continue

        if _run_last_activity(dag) < cutoff:
            # Idle past the timeout — don't repopulate a dead run; mark it
            # cancelled on disk so it doesn't resurface on the next boot.
            try:
                dag.mark_cancelled("idle_timeout")
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "recover_active_runs: failed to expire %s: %s",
                    run_id, exc,
                )
            expired.append(run_id)
            continue

        with _ACTIVE_RUNS_LOCK:
            _ACTIVE_RUNS[run_id] = dag
        recovered.append(run_id)

    logger.info(
        "Workflow run recovery: %d recovered, %d expired (idle), "
        "%d skipped, %d errors",
        len(recovered), len(expired), skipped, errors,
    )
    return {
        "recovered": recovered,
        "expired": expired,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_response(
    run_id: str,
    dag: WorkflowDAG,
    *,
    prior_step_id: str | None = None,
) -> dict[str, Any]:
    """Build the standard response with the next available step.

    If the next available step has ``auto_run`` metadata, the conductor
    executes it transparently (importing and calling the callable),
    stores the result, and loops until it reaches either a non-auto_run
    step (returned to the agent) or workflow completion.

    ``prior_step_id`` (when provided by ``advance_workflow``) is threaded
    into ``_relevant_step_results`` so the just-completed step's result
    is included in ``step_results`` for continuity.  This consolidates
    step_results building inside ``_build_response`` instead of having
    ``advance_workflow`` override it after the fact.
    """
    auto_ran: list[dict[str, Any]] = []  # track what was auto-executed
    wf_def = _get_wf_def(dag)  # for visibility lookups

    while True:
        available = dag.next_available()
        if not available:
            resp = _build_complete_response(run_id, dag)
            if auto_ran:
                resp["auto_ran"] = auto_ran
                resp["step_results"] = _visibility_filter_results(dag)
            return resp

        next_task = available[0]
        task_id = next_task["task_id"]
        meta = next_task.get("metadata", {})

        # --- Tool availability check for steps with requires ---
        step_requires = meta.get("requires", [])
        if step_requires:
            from work_buddy.tools import is_tool_available

            # CP-A4: lazy auto-recovery for stale-unavailable tools.
            # If the cached _TOOL_STATUS reports a tool as unavailable,
            # re-probe it before failing the step. The recheck honours
            # a per-tool cool-down (default 30s) so a workflow with N
            # steps all requiring the same tool only pays the probe
            # cost once. Closes the bootstrap-race papercut for
            # workflows: at sidecar startup the obsidian probe may
            # briefly fail and stale-disable every workflow step that
            # needs it; without recheck, those steps fail forever
            # until a manual mcp_registry_reload.
            #
            # Conditional: only recheck tools currently reporting
            # unavailable. Healthy paths pay no latency cost.
            from work_buddy.recovery import recheck_tool

            missing_tools: list[str] = []
            for t in step_requires:
                if not is_tool_available(t):
                    recheck_tool(t)
                    if not is_tool_available(t):
                        missing_tools.append(t)

            if missing_tools:
                is_optional = meta.get("optional", False)
                if is_optional:
                    dag.start_task(task_id)
                    dag.skip_task(
                        task_id,
                        reason=f"Required tools unavailable: {missing_tools}",
                    )
                    auto_ran.append({
                        "id": task_id,
                        "name": next_task["name"],
                        "skipped": True,
                        "reason": f"Tools unavailable: {missing_tools}",
                    })
                    logger.info(
                        "Skipped optional step '%s' — missing tools: %s",
                        task_id, missing_tools,
                    )
                    continue
                else:
                    # Required step can't run — return error to agent
                    dag.start_task(task_id)
                    dag.fail_task(
                        task_id,
                        error=f"Required tools unavailable: {missing_tools}",
                    )
                    logger.warning(
                        "Required step '%s' failed — missing tools: %s",
                        task_id, missing_tools,
                    )
                    continue

        auto_run_spec = meta.get("auto_run")

        if not auto_run_spec:
            # Normal step — hand to the agent.
            # If this step opted into individual consent, suspend the
            # workflow run grant so the agent's @requires_consent-gated
            # calls actually surface a prompt. The grant is re-minted in
            # advance_workflow once the agent completes this step.
            if meta.get("requires_individual_consent", False):
                from work_buddy.consent import revoke_workflow_run
                wf_name = getattr(dag, "workflow_name", None) or (
                    (getattr(dag, "name", "") or "").split(":", 1)[0]
                    if ":" in (getattr(dag, "name", "") or "")
                    else getattr(dag, "name", "")
                )
                revoke_workflow_run(
                    wf_name,
                    run_id,
                    session_id=getattr(dag, "agent_session_id", None),
                    reason="individual_consent",
                )
                logger.info(
                    "Main-execution step '%s' requires explicit consent — "
                    "workflow run grant temporarily suspended", task_id,
                )
            break

        # --- Auto-execute this step ---
        # If the step requires explicit consent, temporarily suspend the
        # workflow run grant so @requires_consent checks are enforced.
        explicit_consent = meta.get("requires_individual_consent", False)
        if explicit_consent:
            from work_buddy.consent import revoke_workflow_run
            wf_name = getattr(dag, "workflow_name", None) or (
                (getattr(dag, "name", "") or "").split(":", 1)[0]
                if ":" in (getattr(dag, "name", "") or "")
                else getattr(dag, "name", "")
            )
            revoke_workflow_run(
                wf_name,
                run_id,
                session_id=getattr(dag, "agent_session_id", None),
                reason="individual_consent",
            )
            logger.info(
                "Step '%s' requires explicit consent — "
                "workflow run grant temporarily suspended", task_id,
            )

        dag.start_task(task_id)
        result = _execute_auto_run(
            task_id,
            auto_run_spec,
            dag.get_all_results(),
            agent_session_id=getattr(dag, "agent_session_id", None),
            initial_params=getattr(dag, "initial_params", None),
        )

        # Re-mint workflow run grant if we suspended it
        if explicit_consent:
            from work_buddy.consent import grant_workflow_run
            wf_name = getattr(dag, "workflow_name", None) or (
                (getattr(dag, "name", "") or "").split(":", 1)[0]
                if ":" in (getattr(dag, "name", "") or "")
                else getattr(dag, "name", "")
            )
            grant_workflow_run(
                wf_name,
                run_id,
                session_id=getattr(dag, "agent_session_id", None),
            )
            logger.info(
                "Step '%s' done — workflow run grant re-minted", task_id,
            )

        if result.get("success"):
            serialized = _safe_serialize(result["value"])
            dag.complete_task(task_id, result=serialized)
            # ``auto_ran`` is a status ledger ({id, name} on success;
            # {id, name, error} on failure; {id, name, skipped, reason}
            # on skip).  The actual result data lives in step_results[id]
            # — a single canonical surface — visibility-filtered when
            # ``_relevant_step_results`` (or ``_visibility_filter_results``)
            # builds the response.
            auto_ran.append({
                "id": task_id,
                "name": next_task["name"],
            })
            logger.info("Auto-ran step %s -> success", task_id)
        else:
            # Auto-run failed — mark step failed, break out so the agent
            # sees the next available step (or completion) with the error
            error_msg = result.get("error", "unknown error")
            dag.fail_task(task_id, error=error_msg)
            auto_ran.append({
                "id": task_id,
                "name": next_task["name"],
                "error": error_msg,
            })
            logger.warning("Auto-ran step %s -> failed: %s", task_id, error_msg)
            # Re-evaluate: there may be more available steps after failure
            continue

    # We broke out with a non-auto_run step to present to the agent
    dag.start_task(task_id)

    instruction = meta.get("instruction", "")
    step_type = meta.get("step_type", "unknown")

    if not instruction and step_type == "reasoning":
        logger.warning(
            "Reasoning step '%s' has empty instruction — the agent will "
            "receive no procedure. Check that the workflow body has a "
            "matching section (### Task: `%s` or ### N. %s).",
            task_id, task_id, next_task["name"],
        )

    current_step: dict[str, Any] = {
        "id": task_id,
        "name": next_task["name"],
        "instruction": instruction,
        "step_type": step_type,
        "execution": next_task.get("execution", "main"),
    }

    if next_task.get("workflow_file"):
        current_step["workflow_file"] = next_task["workflow_file"]

    total = dag._graph.number_of_nodes()
    completed = sum(
        1 for _, d in dag._graph.nodes(data=True)
        if d.get("status") in ("completed", "skipped")
    )

    response: dict[str, Any] = {
        "type": "workflow_step",
        "workflow_run_id": run_id,
        "current_step": current_step,
        "progress": f"{completed}/{total} steps completed",
        "remaining_steps": [
            t["task_id"] for t in available[1:]
        ],
        "step_results": _relevant_step_results(
            dag,
            task_id,
            prior_step_id=prior_step_id,
            just_ran_step_ids=[ar["id"] for ar in auto_ran],
        ),
        "diagram": _dag_to_mermaid(dag),
    }

    if auto_ran:
        response["auto_ran"] = auto_ran

        # Detect timeout results in auto_ran steps and inject recovery info.
        # Result data is no longer carried in auto_ran[*]; read it from the
        # DAG's stored results.  Timeout-bearing results carry ``timeout: True``
        # and are preserved in full by ``_apply_visibility``.
        all_results = dag.get_all_results()
        timeout_steps = []
        for ar in auto_ran:
            res = all_results.get(ar["id"])
            if isinstance(res, dict) and res.get("timeout"):
                timeout_steps.append((ar, res))
        if timeout_steps:
            response["timeout_recovery"] = {
                "timed_out_steps": [
                    {
                        "step_id": ar["id"],
                        "step_name": ar["name"],
                        "request_id": res.get("request_id", ""),
                        "hint": (
                            f"Step '{ar['id']}' timed out waiting for user "
                            f"response. Options: (1) re-poll via "
                            f"wb_run('request_poll', {{'notification_id': "
                            f"'{res.get('request_id', '')}', "
                            f"'timeout_seconds': 120}}), (2) present the "
                            f"data in chat and collect decisions "
                            f"interactively, (3) check if the user "
                            f"responded late."
                        ),
                    }
                    for ar, res in timeout_steps
                ],
            }

    return response


def _execute_auto_run(
    step_id: str,
    spec: dict[str, Any],
    step_results: dict[str, Any],
    *,
    agent_session_id: str | None = None,
    initial_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute an auto_run callable in an isolated subprocess.

    Spawns ``python -m work_buddy.mcp_server.subprocess_runner`` and
    communicates via JSON over stdin/stdout. This keeps CPU-bound steps
    from holding the GIL in the shared MCP server process.

    Args:
        step_id: The DAG step ID (for logging).
        spec: ``{"callable": "dotted.path", "kwargs": {...}, "timeout": 30}``
        step_results: All completed step results so far (available via
            ``input_map`` wiring).
        agent_session_id: The agent session that started this workflow.
            When provided, the subprocess runs under that session so its
            consent checks read the same consent.db as the agent's own
            grants. Falls back to the MCP server's own env session if
            unset (legacy behavior for non-workflow callers).
        initial_params: Caller-provided params from ``start_workflow``.
            Reachable from ``input_map`` via the ``__params__`` source
            (``__params__`` → whole dict; ``__params__.foo`` → nested key).

    Returns:
        ``{"success": True, "value": <return value>}`` or
        ``{"success": False, "error": "<message>"}``
    """
    dotted_path = spec.get("callable", "")
    kwargs = dict(spec.get("kwargs") or {})
    timeout = spec.get("timeout", 30)

    # --- Safety: only allow work_buddy.* imports ---
    if not dotted_path.startswith("work_buddy."):
        return {
            "success": False,
            "error": (
                f"Import path {dotted_path!r} rejected: "
                "auto_run only allows work_buddy.* callables"
            ),
        }

    # --- Validate callable path format ---
    parts = dotted_path.rsplit(".", 1)
    if len(parts) != 2:
        return {
            "success": False,
            "error": f"Invalid callable path {dotted_path!r}: expected module.function",
        }

    # --- Strip None values from kwargs (YAML `null` → Python None) ---
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    # --- Resolve input_map: wire prior step results into kwargs ---
    # Sources beginning with ``__params__`` resolve from the workflow's
    # caller-provided initial params instead of step_results. Supports
    # dotted-key walks: ``__params__`` → whole dict; ``__params__.foo``
    # → initial_params["foo"]; ``__params__.a.b`` → nested.
    input_map = spec.get("input_map") or {}
    for kwarg_name, source_step_id in input_map.items():
        if isinstance(source_step_id, str) and source_step_id.startswith("__params__"):
            found, value = _resolve_params_source(source_step_id, initial_params)
            if not found:
                return {
                    "success": False,
                    "error": (
                        f"input_map references {source_step_id!r} for kwarg "
                        f"{kwarg_name!r}, but the workflow's initial_params has no "
                        "such key"
                    ),
                }
            kwargs[kwarg_name] = value
            continue
        if source_step_id in step_results:
            kwargs[kwarg_name] = step_results[source_step_id]
        else:
            return {
                "success": False,
                "error": (
                    f"input_map references step {source_step_id!r} for kwarg "
                    f"{kwarg_name!r}, but that step has no result yet"
                ),
            }

    # --- Build subprocess payload ---
    # Prefer the workflow-pinned agent session so consent checks hit the
    # agent's DB; fall back to the MCP server's env for legacy callers.
    effective_session = (
        agent_session_id
        if agent_session_id
        else os.environ.get("WORK_BUDDY_SESSION_ID", "")
    )
    payload = {
        "callable": dotted_path,
        "kwargs": _safe_serialize(kwargs),
        "session_id": effective_session,
    }

    repo_root = Path(__file__).resolve().parent.parent.parent
    cmd = [sys.executable, "-m", "work_buddy.mcp_server.subprocess_runner"]

    logger.info(
        "auto_run[%s]: spawning subprocess for %s (timeout=%ds)",
        step_id, dotted_path, timeout,
    )

    # --- Launch subprocess ---
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(repo_root),
        )
    except subprocess.TimeoutExpired as exc:
        stderr_text = getattr(exc, "stderr", "") or ""
        if stderr_text:
            logger.warning(
                "auto_run[%s]: stderr before timeout:\n%s",
                step_id, stderr_text.strip(),
            )
        logger.warning(
            "auto_run[%s]: %s timed out after %ds", step_id, dotted_path, timeout,
        )
        return {
            "success": False,
            "error": f"auto_run {dotted_path!r} timed out after {timeout}s",
        }

    # --- Log subprocess stderr (diagnostics) ---
    if proc.stderr:
        for line in proc.stderr.strip().splitlines():
            logger.debug("auto_run[%s] stderr: %s", step_id, line)

    # --- Subprocess crashed before writing JSON ---
    if proc.returncode != 0 and not proc.stdout.strip():
        stderr_tail = (proc.stderr or "").strip()[-500:]
        logger.warning(
            "auto_run[%s]: subprocess crashed (exit %d):\n%s",
            step_id, proc.returncode, stderr_tail,
        )
        return {
            "success": False,
            "error": (
                f"auto_run {dotted_path!r} subprocess crashed "
                f"(exit code {proc.returncode}): {stderr_tail}"
            ),
        }

    # --- Parse JSON result from stdout ---
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning(
            "auto_run[%s]: invalid JSON output: %s",
            step_id, proc.stdout[:200],
        )
        return {
            "success": False,
            "error": (
                f"auto_run {dotted_path!r} produced invalid JSON output: "
                f"{proc.stdout[:200]!r}"
            ),
        }

    # --- Log traceback server-side, strip from return ---
    if not result.get("success") and result.get("traceback"):
        logger.warning(
            "auto_run[%s] failed with traceback:\n%s",
            step_id, result["traceback"],
        )
        result.pop("traceback", None)

    logger.info(
        "auto_run[%s]: %s -> %s",
        step_id, dotted_path, "success" if result.get("success") else "failed",
    )

    return result


def _build_complete_response(run_id: str, dag: WorkflowDAG) -> dict[str, Any]:
    """Build the response for a completed workflow."""
    # Persist DAG state BEFORE removing from active runs.  Without this,
    # the DAG file is empty and all step results are lost.
    dag.save()

    # Revoke the workflow run grant now that the workflow is done.
    # Route via the DAG-pinned agent session so we undo the grant we
    # wrote at start_workflow time — otherwise a stale grant would
    # linger in the agent's DB.
    from work_buddy.consent import revoke_workflow_run
    wf_name = getattr(dag, "workflow_name", None) or (
        (getattr(dag, "name", "") or "").split(":", 1)[0] if ":" in (getattr(dag, "name", "") or "") else getattr(dag, "name", "")
    )
    revoke_workflow_run(
        wf_name,
        run_id,
        session_id=getattr(dag, "agent_session_id", None),
        reason="complete",
    )

    total = dag._graph.number_of_nodes()
    return {
        "type": "workflow_complete",
        "workflow_run_id": run_id,
        "summary": dag.summary(),
        "progress": f"{total}/{total} steps completed",
        "step_results": _visibility_filter_results(dag),
        "diagram": _dag_to_mermaid(dag),
    }


def _visibility_filter_results(dag: WorkflowDAG) -> dict[str, Any]:
    """Apply per-step visibility to all DAG results, then cap oversized."""
    wf_def = _get_wf_def(dag)
    all_results = dag.get_all_results()
    visible = {
        k: _apply_visibility(k, v, _get_step_visibility(k, wf_def))
        for k, v in all_results.items()
    }
    return _cap_step_results(visible)


# Maximum serialized size (chars) for a single step result in the MCP response.
# Full results are still in the DAG on disk; this only affects what the agent sees.
_STEP_RESULT_CAP = 50_000


def _cap_step_results(results: dict[str, Any]) -> dict[str, Any]:
    """Truncate oversized step results to keep the MCP response manageable.

    Each step result is JSON-serialized and checked against _STEP_RESULT_CAP.
    Oversized results are replaced with a summary pointing to the DAG file.
    """
    capped: dict[str, Any] = {}
    for step_id, result in results.items():
        try:
            serialized = json.dumps(result)
        except (TypeError, ValueError):
            serialized = str(result)

        if len(serialized) <= _STEP_RESULT_CAP:
            capped[step_id] = result
        else:
            # Preserve the top-level structure hint if it's a dict
            keys_hint = ""
            if isinstance(result, dict):
                keys_hint = f" Keys: {list(result.keys())}"
            capped[step_id] = {
                "_truncated": True,
                "_original_size": len(serialized),
                "_message": (
                    f"Step result too large ({len(serialized):,} chars). "
                    f"Full data is in the DAG state file.{keys_hint}"
                ),
            }
            logger.info(
                "Capped step '%s' result: %s chars -> truncated summary",
                step_id, f"{len(serialized):,}",
            )
    return capped


# ---------------------------------------------------------------------------
# Step result visibility
# ---------------------------------------------------------------------------

# Auto-mode threshold: results under this size are sent in full.
_VISIBILITY_AUTO_THRESHOLD = 10_000


def _make_manifest(
    step_id: str,
    result: Any,
    *,
    include_structure: bool = True,
) -> dict[str, Any]:
    """Build a lightweight manifest describing a step result.

    The manifest tells the agent the result exists, how large it is,
    and what keys are available — without including the data itself.
    The agent can retrieve the full data via ``wb_step_result``.
    """
    try:
        size = len(json.dumps(result, default=str))
    except (TypeError, ValueError):
        size = len(str(result))

    manifest: dict[str, Any] = {
        "_manifest": True,
        "_step_id": step_id,
        "_size": size,
        "_retrievable": True,
    }
    if include_structure and isinstance(result, dict):
        manifest["_keys"] = list(result.keys())
        manifest["_key_sizes"] = {
            k: len(json.dumps(v, default=str))
            for k, v in result.items()
        }
    elif include_structure and isinstance(result, list):
        manifest["_length"] = len(result)
    return manifest


def _apply_visibility(
    step_id: str,
    result: Any,
    visibility: ResultVisibility | None,
) -> Any:
    """Filter a step result according to its visibility spec.

    Returns the (possibly reduced) result for inclusion in the MCP
    response.  The full result is always in the DAG on disk.
    """
    # None / non-dict results pass through unchanged.
    if result is None:
        return result

    # Timeout results are always returned in full — the timeout recovery
    # logic reads ar["result"]["timeout"] and ar["result"]["request_id"].
    if isinstance(result, dict) and result.get("timeout"):
        return result

    vis = visibility or ResultVisibility()  # default: auto
    mode = vis.mode

    # Resolve "auto" based on serialized size.
    if mode == "auto":
        try:
            size = len(json.dumps(result, default=str))
        except (TypeError, ValueError):
            size = len(str(result))
        mode = "full" if size <= _VISIBILITY_AUTO_THRESHOLD else "summary"

    if mode == "full":
        # Still subject to the hard cap.
        try:
            serialized = json.dumps(result)
        except (TypeError, ValueError):
            serialized = str(result)
        if len(serialized) > _STEP_RESULT_CAP:
            return _make_manifest(step_id, result, include_structure=True)
        return result

    if mode == "none":
        return _make_manifest(step_id, result, include_structure=False)

    # mode == "summary"
    if vis.include_keys and isinstance(result, dict):
        # Include only whitelisted keys inline, rest in manifest.
        partial: dict[str, Any] = {
            k: result[k] for k in vis.include_keys if k in result
        }
        manifest = _make_manifest(step_id, result, include_structure=True)
        manifest["_partial"] = partial
        return manifest

    return _make_manifest(step_id, result, include_structure=True)


def _get_step_visibility(
    step_id: str,
    wf_def: WorkflowDefinition | None,
) -> ResultVisibility | None:
    """Look up the visibility spec for a step from its workflow definition."""
    if wf_def is None:
        return None
    for ws in wf_def.steps:
        if ws.id == step_id:
            return ws.visibility
    return None


def _dag_to_mermaid(dag: WorkflowDAG) -> str:
    """Convert a WorkflowDAG to a Mermaid flowchart string.

    Nodes are color-coded by status:
      completed → green, running → orange, available → blue,
      pending/blocked → gray, failed → red, skipped → light gray
    """
    _STATUS_STYLE = {
        "completed": "completed",
        "skipped": "skipped",
        "running": "running",
        "available": "available",
        "pending": "pending",
        "blocked": "pending",
        "failed": "failed",
    }

    # Extract workflow name from DAG name (format: "workflow_name:run_id")
    wf_name = dag.name.split(":")[0] if ":" in dag.name else dag.name

    lines = [
        "---",
        f"title: {wf_name}",
        "---",
        "graph TD",
    ]

    # Node definitions with display labels
    for node_id, data in dag._graph.nodes(data=True):
        name = data.get("name", node_id)
        status = data.get("status", "pending")
        style_class = _STATUS_STYLE.get(status, "pending")
        safe_id = node_id.replace("-", "_")
        lines.append(f"    {safe_id}[\"{name}\"]:::{style_class}")

    # Edges
    for src, dst in dag._graph.edges():
        safe_src = src.replace("-", "_")
        safe_dst = dst.replace("-", "_")
        lines.append(f"    {safe_src} --> {safe_dst}")

    # Style definitions
    lines.append("")
    lines.append("    classDef completed fill:#4CAF50,color:#fff,stroke:#388E3C")
    lines.append("    classDef running fill:#FF9800,color:#fff,stroke:#F57C00")
    lines.append("    classDef available fill:#2196F3,color:#fff,stroke:#1976D2")
    lines.append("    classDef pending fill:#9E9E9E,color:#fff,stroke:#757575")
    lines.append("    classDef failed fill:#F44336,color:#fff,stroke:#D32F2F")
    lines.append("    classDef skipped fill:#BDBDBD,color:#616161,stroke:#9E9E9E")

    return "\n".join(lines)


def _get_wf_def(dag: WorkflowDAG) -> WorkflowDefinition | None:
    """Try to recover the WorkflowDefinition from a running DAG's name."""
    wf_name = dag.name.split(":")[0] if ":" in dag.name else dag.name
    entry = get_entry(wf_name)
    return entry if isinstance(entry, WorkflowDefinition) else None


def _relevant_step_results(
    dag: WorkflowDAG,
    current_step_id: str,
    prior_step_id: str | None = None,
    just_ran_step_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Return only the step results the agent needs right now.

    Includes:
    1. Direct predecessors of the current step (its ``depends_on``)
    2. All ``input_map`` source steps for the downstream auto_run chain
       (from the current step up to and including the next reasoning step)
    3. The prior step result (for continuity)
    4. Any steps that just ran during the current ``_build_response``
       invocation — guarantees the conductor never advertises an auto_run
       step in ``auto_ran`` (the status ledger) without also surfacing
       its data in ``step_results``, even when the next reasoning step
       doesn't declare the auto_run step as a ``depends_on``.

    Falls back to capping all results if no workflow definition is found.
    """
    all_results = dag.get_all_results()

    wf_def = _get_wf_def(dag)
    if wf_def is None:
        return _cap_step_results(all_results)

    # Build step lookup
    step_lookup: dict[str, WorkflowStep] = {s.id: s for s in wf_def.steps}

    needed: set[str] = set()

    # 1. Direct dependencies of the current step
    current_ws = step_lookup.get(current_step_id)
    if current_ws:
        needed.update(current_ws.depends_on)

    # 2. Walk downstream auto_run chain and collect their input_map sources
    if wf_def.steps:
        step_ids = [s.id for s in wf_def.steps]
        try:
            start_idx = step_ids.index(current_step_id)
        except ValueError:
            start_idx = len(step_ids)

        for ws in wf_def.steps[start_idx + 1:]:
            if ws.auto_run is None:
                # Hit next reasoning step — stop
                break
            for source_step_id in ws.auto_run.input_map.values():
                needed.add(source_step_id)

    # 3. Prior step (for continuity)
    if prior_step_id:
        needed.add(prior_step_id)

    # 4. Steps that just ran in this _build_response invocation
    if just_ran_step_ids:
        needed.update(just_ran_step_ids)

    # Filter, apply per-step visibility, then cap any remaining oversized results
    filtered = {k: v for k, v in all_results.items() if k in needed}
    visible = {
        k: _apply_visibility(k, v, _get_step_visibility(k, wf_def))
        for k, v in filtered.items()
    }
    return _cap_step_results(visible)


_TYPE_MAP: dict[str, type] = {
    "dict": dict,
    "list": list,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
}


def _validate_step_result(
    step_id: str,
    result: Any,
    schema: dict[str, Any],
) -> str | None:
    """Validate a step result against a declared schema.

    Returns an error message if validation fails, else ``None``.
    Schema format::

        result_schema:
          required_keys: [groups_by_action, total_groups]
          key_types:
            groups_by_action: dict
            total_groups: int
          min_items:
            units_read: 1       # list / dict / str at this key must have len >= 1
    """
    if not isinstance(result, dict):
        return (
            f"Step '{step_id}' must return a dict, got {type(result).__name__}. "
            f"Value preview: {str(result)[:200]}"
        )

    for key in schema.get("required_keys", []):
        if key not in result:
            base = (
                f"Step '{step_id}' missing required key '{key}'. "
                f"Got keys: {list(result.keys())}"
            )
            # When the result is an empty dict, the most likely cause is
            # that the caller never set ``step_result`` at all — either
            # they omitted it or named the parameter incorrectly (e.g.
            # ``result=`` instead of ``step_result=``), which FastMCP
            # silently drops. The keys-based "you forgot a field" framing
            # misdirects in that case, so append a parameter-name hint
            # inline. Safe to always append: an agent who genuinely sent
            # ``{}`` is still in an error path here and the hint still
            # tells them what shape the call needs.
            if not result:
                base += (
                    ". The result arrived empty — call as "
                    "`wb_advance(workflow_run_id=..., step_result={...})`. "
                    "A common mistake is naming the parameter `result=` "
                    "instead of `step_result=`; FastMCP silently drops "
                    "unknown kwargs, so the conductor sees no result."
                )
            return base

    for key, expected_type_name in schema.get("key_types", {}).items():
        if key not in result:
            continue
        expected_type = _TYPE_MAP.get(expected_type_name)
        if expected_type and not isinstance(result[key], expected_type):
            return (
                f"Step '{step_id}' key '{key}' must be {expected_type_name}, "
                f"got {type(result[key]).__name__}"
            )

    for key, min_count in schema.get("min_items", {}).items():
        if key not in result:
            continue
        value = result[key]
        try:
            actual = len(value)
        except TypeError:
            return (
                f"Step '{step_id}' key '{key}' has no length "
                f"(got {type(value).__name__}), cannot check min_items"
            )
        if actual < min_count:
            return (
                f"Step '{step_id}' key '{key}' has {actual} item(s), "
                f"schema requires at least {min_count}. "
                f"Re-read the step instructions and try again with real content."
            )

    return None


def _warn_if_accumulating(dag: WorkflowDAG, completed_step_id: str) -> None:
    """Log a sidecar warning when the just-completed step's result contains
    an upstream step's result as a key-by-key subset.

    This surfaces the cross-step accumulation pattern (Problem C) — each
    step echoing the prior step's fields plus its own delta, which silently
    inflates ``step_results`` over a multi-step workflow.  The warning
    points at the offending step pair and suggests the right fix (use a
    new key for modified values).  Detection only — no automatic
    stripping; the conductor never mutates step results behind the agent's
    back.

    Cost: O(N^2) JSON comparisons across step_results, gated by a
    min-size threshold so trivial accumulations don't pay the cost.  Runs
    once per ``advance_workflow`` call.
    """
    try:
        all_results = dag.get_all_results()
    except Exception:  # noqa: BLE001 — defensive; warning is best-effort
        return
    if not isinstance(all_results, dict) or len(all_results) < 2:
        return

    accumulations = find_step_result_accumulations(
        all_results,
        min_size=DEFAULT_MIN_SUBTREE,
        min_keys=DEFAULT_MIN_CONTAINED_KEYS,
    )
    # Filter to accumulations involving the just-completed step.  Other
    # pairs (between two prior steps) would have been logged on their own
    # completion already, so re-warning on every advance would be noisy.
    relevant = [
        (a, b, sz)
        for a, b, sz in accumulations
        if completed_step_id in (a, b)
    ]
    if not relevant:
        return

    for upstream_id, downstream_id, size in relevant:
        logger.warning(
            "Step '%s' result contains step '%s' as a subtree (%d chars). "
            "Step results should be deltas — return only your new fields. "
            "If you intentionally modified the upstream data, use a new "
            "key name (e.g., 'annotated_<key>') instead of echoing the "
            "same key.",
            downstream_id, upstream_id, size,
        )


def _safe_serialize(obj: Any) -> Any:
    """Make an object JSON-safe for storage as a DAG task result."""
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
