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
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.mcp_server.registry import (
    ResultVisibility, WorkflowDefinition, WorkflowStep, get_entry,
)
from work_buddy.workflow import TaskStatus, WorkflowDAG

logger = logging.getLogger(__name__)


# In-memory map of active workflow runs.
# Key: workflow_run_id, Value: WorkflowDAG instance
_ACTIVE_RUNS: dict[str, WorkflowDAG] = {}


def start_workflow(
    workflow_name: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a workflow and return its first available step.

    Returns a dict with:
      - workflow_run_id
      - workflow_context (philosophy, "What NOT to do" — first step only)
      - current_step (with instruction, workflow_file if applicable)
      - diagram (Mermaid flowchart)
    """
    entry = get_entry(workflow_name)
    if entry is None:
        return {"error": f"Unknown workflow: {workflow_name!r}"}
    if not isinstance(entry, WorkflowDefinition):
        return {"error": f"{workflow_name!r} is a function, not a workflow. Use wb_run to execute it."}
    if not entry.steps:
        return {"error": f"Workflow {workflow_name!r} has no steps defined in frontmatter."}

    run_id = f"wf_{uuid.uuid4().hex[:8]}"

    dag = WorkflowDAG(
        name=f"{workflow_name}:{run_id}",
        description=f"Run of workflow {workflow_name}",
    )

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

    dag.save()
    _ACTIVE_RUNS[run_id] = dag

    # Grant blanket consent for the workflow's lifetime.  Accepting a
    # workflow implies consent for all its operations unless a step
    # explicitly opts out via ``requires_individual_consent: true``.
    from work_buddy.consent import grant_workflow_consent
    grant_workflow_consent(run_id)

    # Build response with workflow context on first step
    response = _build_response(run_id, dag)

    # Include workflow-level context (philosophy, constraints) on start only
    if entry.context:
        response["workflow_context"] = entry.context

    return response


def advance_workflow(
    workflow_run_id: str,
    step_result: Any | None = None,
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
            return {
                "type": "validation_error",
                "workflow_run_id": workflow_run_id,
                "step_id": current_id,
                "error": validation_error,
                "hint": (
                    "Re-read the step instructions carefully. Your step result "
                    "must be the complete data structure (e.g. the full "
                    "presentation dict), not a summary or file reference. "
                    "Call wb_advance again with the correct result."
                ),
                "diagram": _dag_to_mermaid(dag),
            }

    dag.complete_task(current_id, result=serialized_result)

    # Check if workflow is now complete
    if dag.is_complete():
        result = _build_complete_response(workflow_run_id, dag)
        del _ACTIVE_RUNS[workflow_run_id]
        return result

    # Build response — auto_run steps are consumed transparently inside.
    # The auto_run chain may complete the workflow, producing a
    # ``workflow_complete`` response.
    response = _build_response(workflow_run_id, dag)

    if response.get("type") == "workflow_complete":
        # Auto-run chain finished the workflow — clean up
        _ACTIVE_RUNS.pop(workflow_run_id, None)
        return response

    response["prior_step"] = {
        "id": current_id,
        "result": serialized_result,
    }
    # Override step_results with smart trimming (includes prior step)
    next_step_id = response.get("current_step", {}).get("id", "")
    response["step_results"] = _relevant_step_results(
        dag, next_step_id, prior_step_id=current_id,
    )
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
    return [
        {
            "workflow_run_id": run_id,
            "name": dag.name,
            "is_complete": dag.is_complete(),
        }
        for run_id, dag in _ACTIVE_RUNS.items()
    ]


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


def _load_dag_from_disk(workflow_run_id: str) -> WorkflowDAG | None:
    """Try to load a DAG from persisted state files.

    Searches the current session's workflow directory for a DAG whose
    name contains the ``workflow_run_id``.
    """
    from work_buddy.agent_session import get_agent_dir

    wf_dir = get_agent_dir() / "workflows"
    if not wf_dir.is_dir():
        return None

    for path in wf_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            dag_name = data.get("name", "")
            if workflow_run_id in dag_name:
                return WorkflowDAG.load(path)
        except Exception:
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
# Internal helpers
# ---------------------------------------------------------------------------

def _build_response(
    run_id: str,
    dag: WorkflowDAG,
) -> dict[str, Any]:
    """Build the standard response with the next available step.

    If the next available step has ``auto_run`` metadata, the conductor
    executes it transparently (importing and calling the callable),
    stores the result, and loops until it reaches either a non-auto_run
    step (returned to the agent) or workflow completion.
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
            missing_tools = [t for t in step_requires if not is_tool_available(t)]
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
            # Normal step — hand to the agent
            break

        # --- Auto-execute this step ---
        # If the step requires explicit consent, temporarily suspend the
        # workflow blanket so @requires_consent checks are enforced.
        explicit_consent = meta.get("requires_individual_consent", False)
        if explicit_consent:
            from work_buddy.consent import revoke_workflow_consent
            revoke_workflow_consent(run_id)
            logger.info(
                "Step '%s' requires explicit consent — "
                "workflow blanket temporarily suspended", task_id,
            )

        dag.start_task(task_id)
        result = _execute_auto_run(
            task_id,
            auto_run_spec,
            dag.get_all_results(),
        )

        # Re-grant workflow consent if we suspended it
        if explicit_consent:
            from work_buddy.consent import grant_workflow_consent
            grant_workflow_consent(run_id)
            logger.info(
                "Step '%s' done — workflow blanket re-granted", task_id,
            )

        if result.get("success"):
            serialized = _safe_serialize(result["value"])
            dag.complete_task(task_id, result=serialized)
            vis = _get_step_visibility(task_id, wf_def)
            auto_ran.append({
                "id": task_id,
                "name": next_task["name"],
                "result": _apply_visibility(task_id, serialized, vis),
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
        "step_results": _relevant_step_results(dag, task_id),
        "diagram": _dag_to_mermaid(dag),
    }

    if auto_ran:
        response["auto_ran"] = auto_ran

        # Detect timeout results in auto_ran steps and inject recovery info
        timeout_steps = [
            ar for ar in auto_ran
            if isinstance(ar.get("result"), dict) and ar["result"].get("timeout")
        ]
        if timeout_steps:
            response["timeout_recovery"] = {
                "timed_out_steps": [
                    {
                        "step_id": ts["id"],
                        "step_name": ts["name"],
                        "request_id": ts["result"].get("request_id", ""),
                        "hint": (
                            f"Step '{ts['id']}' timed out waiting for user "
                            f"response. Options: (1) re-poll via "
                            f"wb_run('request_poll', {{'notification_id': "
                            f"'{ts['result'].get('request_id', '')}', "
                            f"'timeout_seconds': 120}}), (2) present the "
                            f"data in chat and collect decisions "
                            f"interactively, (3) check if the user "
                            f"responded late."
                        ),
                    }
                    for ts in timeout_steps
                ],
            }

    return response


def _execute_auto_run(
    step_id: str,
    spec: dict[str, Any],
    step_results: dict[str, Any],
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
    input_map = spec.get("input_map") or {}
    for kwarg_name, source_step_id in input_map.items():
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
    payload = {
        "callable": dotted_path,
        "kwargs": _safe_serialize(kwargs),
        "session_id": os.environ.get("WORK_BUDDY_SESSION_ID", ""),
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

    # Revoke workflow blanket consent now that the workflow is done.
    from work_buddy.consent import revoke_workflow_consent
    revoke_workflow_consent(run_id)

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
) -> dict[str, Any]:
    """Return only the step results the agent needs right now.

    Includes:
    1. Direct predecessors of the current step (its ``depends_on``)
    2. All ``input_map`` source steps for the downstream auto_run chain
       (from the current step up to and including the next reasoning step)
    3. The prior step result (for continuity)

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
    """
    if not isinstance(result, dict):
        return (
            f"Step '{step_id}' must return a dict, got {type(result).__name__}. "
            f"Value preview: {str(result)[:200]}"
        )

    for key in schema.get("required_keys", []):
        if key not in result:
            return (
                f"Step '{step_id}' missing required key '{key}'. "
                f"Got keys: {list(result.keys())}"
            )

    for key, expected_type_name in schema.get("key_types", {}).items():
        if key not in result:
            continue
        expected_type = _TYPE_MAP.get(expected_type_name)
        if expected_type and not isinstance(result[key], expected_type):
            return (
                f"Step '{step_id}' key '{key}' must be {expected_type_name}, "
                f"got {type(result[key]).__name__}"
            )

    return None


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
