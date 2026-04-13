"""Job executor — runs capabilities, workflows, and agent sessions.

This module handles the actual execution of jobs dispatched by the
scheduler or the message poller. Three execution paths:

1. **capability**: Look up in MCP registry, call directly (Tier 1).
2. **workflow**: Start the workflow DAG, auto-advance code steps,
   spawn agent for reasoning steps (Tier 1 + Tier 3).
3. **prompt**: Spawn a ``claude -p`` one-shot agent session (Tier 3).

Agent spawning (Tier 3) is consent-gated. Without a valid consent
grant for ``sidecar:agent_spawn``, prompt and reasoning-step jobs
are logged but not executed.

Spawn modes (Tier 3)
--------------------
- ``headless_ephemeral``: Fire-and-forget, no session persistence.
- ``headless_persistent``: Session state saved, registered for future
  resume/routing. Currently one-write: the initial session context is
  preserved, but callback resumes use ``--no-session-persistence``.
- ``interactive_persistent``: User-visible interactive session
  launched via hidden PTY. Appears in the Claude Code session picker.

See ``dispatch/models.py`` for the full spawn type model.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.sidecar.dispatch.models import AgentTarget, SpawnMode, SpawnResult
from work_buddy.sidecar.scheduler.jobs import Job

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent.parent

# Consent operation ID for Tier 3 agent spawning
AGENT_SPAWN_CONSENT_OP = "sidecar:agent_spawn"

# Session name prefix for daemon-spawned agents.
# Phase C (routing) uses this to distinguish daemon agents from user sessions.
DAEMON_SESSION_PREFIX = "daemon:"


def execute_job(job: Job) -> dict[str, Any]:
    """Execute a job based on its type.

    Args:
        job: The Job to execute.

    Returns:
        Dict with at least ``status`` (``"ok"`` | ``"error"`` |
        ``"consent_required"``) and optional ``result`` or ``error`` keys.
    """
    if job.job_type == "capability":
        return _execute_capability(job.capability, job.params)
    elif job.job_type == "workflow":
        return _execute_workflow(job.workflow)
    elif job.job_type == "prompt":
        return _execute_prompt(job.name, job.prompt, spawn_mode_str=job.spawn_mode)
    else:
        return {"status": "error", "error": f"Unknown job type: {job.job_type}"}


def _execute_capability(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Execute a registered MCP gateway capability by name.

    The sidecar runs in its own process so it CAN import heavy libs
    (unlike the MCP server which has asyncio import deadlock constraints).
    """
    if not name:
        return {"status": "error", "error": "No capability name specified."}

    try:
        from work_buddy.mcp_server.registry import get_registry, Capability

        registry = get_registry()
        entry = registry.get(name)

        if entry is None:
            return {"status": "error", "error": f"Capability '{name}' not found in registry."}

        if not isinstance(entry, Capability):
            return {"status": "error", "error": f"'{name}' is a workflow, not a capability. Use job_type='workflow'."}

        logger.debug("Executing capability: %s(%s)", name, params)
        result = entry.callable(**params)
        return {"status": "ok", "result": result}

    except Exception as exc:
        logger.error("Capability '%s' failed: %s", name, exc, exc_info=True)
        return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Workflow execution (Tier 1 code steps + Tier 3 reasoning steps)
# ---------------------------------------------------------------------------


def _execute_workflow(name: str) -> dict[str, Any]:
    """Execute a registered workflow by name.

    Auto-advances ``step_type="code"`` steps by calling the matching
    capability.  ``step_type="reasoning"`` steps require an agent and
    are executed via ``_spawn_agent()`` (consent-gated).

    Steps that are neither code nor reasoning are skipped with a note.
    """
    if not name:
        return {"status": "error", "error": "No workflow name specified."}

    try:
        from work_buddy.mcp_server.registry import get_registry, WorkflowDefinition
        from work_buddy.mcp_server.conductor import start_workflow, advance_workflow

        registry = get_registry()
        entry = registry.get(name)

        if entry is None:
            return {"status": "error", "error": f"Workflow '{name}' not found in registry."}

        if not isinstance(entry, WorkflowDefinition):
            return {"status": "error", "error": f"'{name}' is a capability, not a workflow. Use job_type='capability'."}

        if not entry.steps:
            return {"status": "error", "error": f"Workflow '{name}' has no steps defined."}

        # Start the workflow DAG
        response = start_workflow(name)
        if "error" in response:
            return {"status": "error", "error": response["error"]}

        run_id = response["workflow_run_id"]
        logger.info("Started workflow '%s' (run_id=%s, steps=%d)", name, run_id, len(entry.steps))

        completed_steps = 0
        skipped_steps = 0
        agent_steps = 0
        step_results: list[dict[str, Any]] = []

        # Walk the DAG, auto-advancing what we can
        while True:
            current = response.get("current_step")
            if current is None or response.get("type") == "workflow_complete":
                break

            step_id = current["id"]
            step_type = current.get("step_type", "reasoning")
            step_name = current.get("name", step_id)

            if step_type == "code":
                # Code steps: look up the step_id as a capability and call it
                result = _execute_code_step(step_id, step_name)
                step_results.append({"step": step_id, "type": "code", "result": result})
                completed_steps += 1
                response = advance_workflow(run_id, step_result=result)

            elif step_type == "reasoning":
                # Reasoning steps: need an agent session (Tier 3)
                instruction = current.get("instruction", "")
                prompt = _build_reasoning_prompt(name, step_name, instruction, step_results)
                agent_result = _spawn_agent(
                    name=f"wf:{name}:{step_id}",
                    prompt=prompt,
                )

                if agent_result["status"] == "consent_required":
                    # Can't proceed without consent — log remaining steps
                    logger.info(
                        "Workflow '%s' paused at reasoning step '%s' "
                        "(agent spawn requires consent). Completed %d code steps.",
                        name, step_id, completed_steps,
                    )
                    step_results.append({
                        "step": step_id,
                        "type": "reasoning",
                        "result": "Consent required for agent spawn — step deferred.",
                    })
                    agent_steps += 1
                    # Advance with a note so the DAG records the deferral
                    response = advance_workflow(
                        run_id,
                        step_result="Deferred: agent spawn requires consent.",
                    )
                elif agent_result["status"] == "ok":
                    step_results.append({
                        "step": step_id,
                        "type": "reasoning",
                        "result": agent_result.get("result", ""),
                    })
                    completed_steps += 1
                    response = advance_workflow(run_id, step_result=agent_result.get("result"))
                else:
                    # Error — advance with error note
                    step_results.append({
                        "step": step_id,
                        "type": "reasoning",
                        "result": f"Error: {agent_result.get('error', 'unknown')}",
                    })
                    skipped_steps += 1
                    response = advance_workflow(
                        run_id,
                        step_result=f"Error: {agent_result.get('error', 'unknown')}",
                    )
            else:
                # Unknown step type — skip
                logger.warning("Unknown step_type '%s' for step '%s' — skipping.", step_type, step_id)
                skipped_steps += 1
                response = advance_workflow(run_id, step_result="Skipped: unknown step type.")

        summary = (
            f"Workflow '{name}' finished: "
            f"{completed_steps} completed, {skipped_steps} skipped, "
            f"{agent_steps} deferred (consent)."
        )
        logger.info(summary)
        return {
            "status": "ok",
            "result": summary,
            "steps": step_results,
            "run_id": run_id,
        }

    except Exception as exc:
        logger.error("Workflow '%s' failed: %s", name, exc, exc_info=True)
        return {"status": "error", "error": str(exc)}


def _execute_code_step(step_id: str, step_name: str) -> Any:
    """Execute a code step by looking up step_id as a capability.

    Falls back to returning a placeholder if no matching capability exists.
    """
    try:
        from work_buddy.mcp_server.registry import get_registry, Capability

        registry = get_registry()
        entry = registry.get(step_id)

        if entry is not None and isinstance(entry, Capability):
            logger.info("Code step '%s' → capability '%s'", step_name, step_id)
            return entry.callable()

        # No matching capability — just note it
        logger.info(
            "Code step '%s' has no matching capability '%s' — passing through.",
            step_name, step_id,
        )
        return f"Code step '{step_name}' executed (no capability match for '{step_id}')."

    except Exception as exc:
        logger.error("Code step '%s' failed: %s", step_name, exc, exc_info=True)
        return f"Error in code step '{step_name}': {exc}"


def _build_reasoning_prompt(
    workflow_name: str,
    step_name: str,
    instruction: str,
    prior_results: list[dict[str, Any]],
) -> str:
    """Build a prompt for an agent to execute a reasoning step."""
    from work_buddy.prompts import get_prompt

    return get_prompt(
        "reasoning_step",
        workflow_name=workflow_name,
        step_name=step_name,
        instruction=instruction,
        prior_results=prior_results,
    )


# ---------------------------------------------------------------------------
# Agent spawning (Tier 3) — consent-gated
# ---------------------------------------------------------------------------


def _execute_prompt(
    name: str,
    prompt: str,
    spawn_mode_str: str = "",
) -> dict[str, Any]:
    """Handle a freeform prompt job by spawning a ``claude -p`` session.

    Consent-gated: requires a valid grant for ``sidecar:agent_spawn``.
    Without consent, the prompt is logged but not executed.

    Args:
        name: Job name.
        prompt: Prompt text.
        spawn_mode_str: Optional spawn mode from job frontmatter.
            Empty string uses the config default (headless_ephemeral).
    """
    if not prompt:
        return {"status": "error", "error": "Empty prompt."}

    mode = _resolve_spawn_mode(spawn_mode_str)
    return _spawn_agent(name=name, prompt=prompt, spawn_mode=mode)


def _resolve_spawn_mode(mode_str: str) -> SpawnMode:
    """Resolve a spawn mode string to a SpawnMode enum.

    Falls back to config default, then to headless_ephemeral.
    """
    if mode_str:
        try:
            return SpawnMode(mode_str)
        except ValueError:
            logger.warning("Invalid spawn_mode '%s' — using default.", mode_str)

    cfg = load_config()
    default = (
        cfg.get("sidecar", {})
        .get("agent_spawn", {})
        .get("default_spawn_mode", "headless_ephemeral")
    )
    try:
        return SpawnMode(default)
    except ValueError:
        return SpawnMode.HEADLESS_EPHEMERAL


def _spawn_agent(
    *,
    name: str,
    prompt: str,
    spawn_mode: SpawnMode = SpawnMode.HEADLESS_EPHEMERAL,
    timeout_seconds: int | None = None,
    max_budget_usd: float | None = None,
) -> dict[str, Any]:
    """Spawn an agent session (Tier 3).

    This is the sidecar's agent execution primitive — for event-triggered,
    cron-scheduled, or message-dispatched work where no active agent
    exists to act as a parent.  This is NOT for agent-to-agent calls;
    agents should use Claude Code's native subagent/Agent tool for that.

    Consent-gated: requires a valid grant for ``sidecar:agent_spawn``.

    On Windows, uses ``CREATE_NO_WINDOW`` (not ``DETACHED_PROCESS``)
    so the child dies when the sidecar dies.

    All sessions are tagged with ``--name daemon:<job-name>`` so they
    are distinguishable from user-initiated sessions.  This is critical
    for future daemon-agent routing (Phase C): the router must avoid
    hijacking user sessions.

    Spawn modes:
        - ``HEADLESS_EPHEMERAL``: ``claude -p`` + ``--no-session-persistence``.
          Fire-and-forget. No registry entry.
        - ``HEADLESS_PERSISTENT``: ``claude -p`` without
          ``--no-session-persistence``. Session is saved; ``session_id``
          is captured and written to the agent registry for future
          resume/routing.
        - ``INTERACTIVE_PERSISTENT``: Spawned via hidden PTY, visible in
          the user's Claude Code session picker.

    Args:
        name: Job name for logging/tracking.
        prompt: The prompt text to send to ``claude -p``.
        spawn_mode: How to launch the session. See :class:`SpawnMode`.
        timeout_seconds: Max execution time. Defaults to config or 300s.
        max_budget_usd: Cost ceiling. Defaults to config or $0.50.

    Returns:
        Dict with status, stdout, stderr, timing info, and for persistent
        modes a ``spawn_result`` key containing the serialized
        :class:`SpawnResult`.
    """
    # --- Interactive mode: PTY-based spawn ---
    if spawn_mode == SpawnMode.INTERACTIVE_PERSISTENT:
        return _spawn_interactive_agent(
            name=name,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            max_budget_usd=max_budget_usd,
        )

    # --- Consent gate ---
    if not _check_agent_spawn_consent():
        logger.info(
            "Agent spawn blocked (no consent): job='%s', prompt_len=%d. "
            "Grant consent for '%s' to enable.",
            name, len(prompt), AGENT_SPAWN_CONSENT_OP,
        )
        return {
            "status": "consent_required",
            "result": (
                f"Agent spawn for '{name}' requires consent. "
                f"Grant '{AGENT_SPAWN_CONSENT_OP}' to enable Tier 3 execution."
            ),
        }

    # --- Billing mode check ---
    # Warn if ANTHROPIC_API_KEY is set (non-empty), because that switches
    # claude -p from subscription billing to pay-per-token API billing.
    # This is a safety net: an agent should never silently set this key.
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        logger.warning(
            "ANTHROPIC_API_KEY is set — agent spawns will use API billing "
            "(pay-per-token), NOT your Claude subscription. If this is "
            "unintended, unset the variable."
        )

    # --- Load config ---
    cfg = load_config()
    agent_cfg = cfg.get("sidecar", {}).get("agent_spawn", {})

    if timeout_seconds is None:
        timeout_seconds = agent_cfg.get("timeout_seconds", 300)
    if max_budget_usd is None:
        max_budget_usd = agent_cfg.get("max_budget_usd", 0.50)

    model = agent_cfg.get("model", "sonnet")

    # --- Build session name ---
    # Convention: "daemon:<job-name>" so daemon-spawned sessions are
    # distinguishable from user-initiated ones.  Phase C (routing) will
    # use this prefix to avoid hijacking user sessions.
    session_name = f"{DAEMON_SESSION_PREFIX}{name}"

    # --- Build command ---
    # Note: Do NOT use --bare — it disables OAuth/keychain, causing auth failure.
    cmd = [
        "claude",
        "--print",
        "--model", model,
        "--output-format", "json",
        "--max-budget-usd", str(max_budget_usd),
        "--dangerously-skip-permissions",
        "--name", session_name,
    ]

    if not spawn_mode.is_persistent:
        cmd.append("--no-session-persistence")

    cmd.append(prompt)

    logger.info(
        "Spawning agent: job='%s', mode=%s, model=%s, timeout=%ds, "
        "budget=$%.2f, prompt_len=%d",
        name, spawn_mode.value, model, timeout_seconds,
        max_budget_usd, len(prompt),
    )

    # --- Execute ---
    start_time = time.time()
    try:
        from work_buddy.compat import subprocess_creation_flags
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(_REPO_ROOT),
            creationflags=subprocess_creation_flags(),
        )

        elapsed = time.time() - start_time
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        # Parse JSON output to extract session_id and result text
        session_id_out = None
        result_text = stdout
        if stdout:
            try:
                import json as _json
                parsed = _json.loads(stdout)
                session_id_out = parsed.get("session_id")
                # The actual response text is in the 'result' field
                result_text = parsed.get("result", stdout)
            except (ValueError, TypeError):
                pass  # Not valid JSON — use raw stdout

        # --- Build structured spawn result ---
        spawn_result = SpawnResult.for_mode(
            spawn_mode,
            session_name=session_name,
            session_id=session_id_out,
            source_job=name,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        spawn_result.elapsed_seconds = round(elapsed, 1)
        spawn_result.return_code = result.returncode

        if result.returncode == 0:
            spawn_result.status = "completed"
            spawn_result.result_text = result_text

            # Register persistent sessions for future resume/routing
            if spawn_mode.is_persistent and session_id_out:
                _register_spawn(spawn_result)

            logger.info(
                "Agent completed: job='%s', mode=%s, elapsed=%.1fs, "
                "output_len=%d, session=%s",
                name, spawn_mode.value, elapsed,
                len(result_text), session_id_out,
            )
            return {
                "status": "ok",
                "result": result_text,
                "session_id": session_id_out,
                "spawn_result": spawn_result.to_dict(),
                "stderr": stderr if stderr else None,
                "elapsed_seconds": round(elapsed, 1),
                "return_code": 0,
            }
        else:
            spawn_result.status = "failed"
            spawn_result.error = f"claude -p exited with code {result.returncode}"

            logger.warning(
                "Agent failed: job='%s', mode=%s, rc=%d, elapsed=%.1fs, "
                "stderr=%s",
                name, spawn_mode.value, result.returncode,
                elapsed, stderr[:500],
            )
            return {
                "status": "error",
                "error": f"claude -p exited with code {result.returncode}",
                "spawn_result": spawn_result.to_dict(),
                "stderr": stderr[:2000] if stderr else None,
                "stdout": stdout[:2000] if stdout else None,
                "elapsed_seconds": round(elapsed, 1),
                "return_code": result.returncode,
            }

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        logger.warning(
            "Agent timed out: job='%s', mode=%s, timeout=%ds",
            name, spawn_mode.value, timeout_seconds,
        )
        return {
            "status": "error",
            "error": f"Agent timed out after {timeout_seconds}s",
            "elapsed_seconds": round(elapsed, 1),
        }
    except FileNotFoundError:
        logger.error(
            "claude CLI not found on PATH. Ensure Claude Code is installed."
        )
        return {
            "status": "error",
            "error": "claude CLI not found on PATH.",
        }
    except Exception as exc:
        elapsed = time.time() - start_time
        logger.error("Agent spawn failed: job='%s', error=%s", name, exc, exc_info=True)
        return {
            "status": "error",
            "error": str(exc),
            "elapsed_seconds": round(elapsed, 1),
        }


def _spawn_interactive_agent(
    *,
    name: str,
    prompt: str,
    timeout_seconds: int | None = None,
    max_budget_usd: float | None = None,
) -> dict[str, Any]:
    """Spawn an interactive persistent Claude session via PTY.

    This creates a real interactive session visible in the user's
    Claude Code Desktop/CLI session picker. The mechanism:

    1. Spawn ``claude "prompt" --name daemon:<name>`` inside a hidden
       pseudo-terminal (ConPTY on Windows, pty on POSIX).
    2. Wait for Claude to produce its initial response.
    3. Parse the session_id from the output.
    4. Close the PTY — the session persists in Claude's storage and
       remains visible in the picker for the user to continue.

    This is consent-gated like all Tier 3 spawns.
    """
    # --- Consent gate ---
    if not _check_agent_spawn_consent():
        logger.info(
            "Interactive agent spawn blocked (no consent): job='%s'. "
            "Grant consent for '%s' to enable.",
            name, AGENT_SPAWN_CONSENT_OP,
        )
        return {
            "status": "consent_required",
            "result": (
                f"Interactive agent spawn for '{name}' requires consent. "
                f"Grant '{AGENT_SPAWN_CONSENT_OP}' to enable."
            ),
        }

    # --- Billing mode check ---
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        logger.warning(
            "ANTHROPIC_API_KEY is set — agent spawns will use API billing."
        )

    # --- Load config ---
    cfg = load_config()
    agent_cfg = cfg.get("sidecar", {}).get("agent_spawn", {})

    if timeout_seconds is None:
        timeout_seconds = agent_cfg.get("timeout_seconds", 300)
    if max_budget_usd is None:
        max_budget_usd = agent_cfg.get("max_budget_usd", 0.50)

    model = agent_cfg.get("model", "sonnet")
    session_name = f"{DAEMON_SESSION_PREFIX}{name}"

    # --- Build command ---
    # Interactive mode: NO --print, NO --output-format json.
    # The prompt is a positional arg: claude "initial prompt"
    # This creates a real interactive session visible in the picker.
    cmd = [
        "claude",
        prompt,
        "--model", model,
        "--max-budget-usd", str(max_budget_usd),
        "--dangerously-skip-permissions",
        "--name", session_name,
    ]

    logger.info(
        "Spawning interactive agent via PTY: job='%s', model=%s, "
        "timeout=%ds, budget=$%.2f, prompt_len=%d",
        name, model, timeout_seconds, max_budget_usd, len(prompt),
    )

    # --- PTY spawn ---
    start_time = time.time()
    pty_session = None
    try:
        from work_buddy.sidecar.dispatch.pty_adapter import PTYSession

        pty_session = PTYSession.spawn(cmd, cwd=str(_REPO_ROOT))

        # Response detection: stability-based.
        #
        # Claude produces terminal output as it starts up and processes
        # the prompt. We wait for output to stabilize (no new data for
        # a quiet period after seeing substantial content). This is
        # more robust than pattern-matching terminal escape sequences.
        min_wait_seconds = 10.0   # Claude needs time to load + respond
        quiet_threshold = 5.0     # Seconds of silence = "done"
        min_output_for_quiet = 500  # Don't trigger on just the banner

        logger.debug(
            "Waiting for response (min_wait=%.0fs, quiet=%.0fs)...",
            min_wait_seconds, quiet_threshold,
        )

        output = ""
        last_output_time = time.time()
        start_reading = time.time()

        while True:
            now = time.time()
            wall_elapsed = now - start_reading

            if wall_elapsed > float(timeout_seconds):
                logger.warning("Interactive agent hit timeout at %.0fs", wall_elapsed)
                break

            chunk = pty_session.read_available(timeout=1.0)
            if chunk:
                output += chunk
                last_output_time = now

            time_since_last = now - last_output_time
            past_min_wait = wall_elapsed >= min_wait_seconds
            has_content = len(output) >= min_output_for_quiet

            if past_min_wait and has_content and time_since_last >= quiet_threshold:
                logger.debug(
                    "Output stabilized: %d chars, quiet for %.1fs",
                    len(output), time_since_last,
                )
                break

            if not pty_session.is_alive():
                logger.warning("PTY process died during response wait")
                break

        elapsed = time.time() - start_time

        # Grace period: let Claude flush conversation data to disk
        # before we close the PTY.
        time.sleep(2.0)

        # --- Extract session_id ---
        # Interactive mode doesn't print session IDs to the terminal.
        # Retrieve it by querying Claude's session list by name.
        session_id_out = _get_session_id_by_name(session_name)

        # --- Build result ---
        spawn_result = SpawnResult.for_mode(
            SpawnMode.INTERACTIVE_PERSISTENT,
            session_name=session_name,
            session_id=session_id_out,
            source_job=name,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        spawn_result.status = "completed"
        spawn_result.result_text = output[-2000:] if len(output) > 2000 else output
        spawn_result.elapsed_seconds = round(elapsed, 1)

        # Register for future resume/routing
        if session_id_out:
            _register_spawn(spawn_result)

        logger.info(
            "Interactive agent completed initial response: job='%s', "
            "elapsed=%.1fs, output_len=%d, session=%s",
            name, elapsed, len(output), session_id_out,
        )

        return {
            "status": "ok",
            "result": output[-2000:] if len(output) > 2000 else output,
            "session_id": session_id_out,
            "session_name": session_name,
            "spawn_result": spawn_result.to_dict(),
            "elapsed_seconds": round(elapsed, 1),
            "visible": True,
        }

    except ImportError as exc:
        elapsed = time.time() - start_time
        logger.error("PTY backend not available: %s", exc)
        return {
            "status": "error",
            "error": f"PTY backend not available: {exc}",
            "elapsed_seconds": round(elapsed, 1),
        }
    except TimeoutError as exc:
        elapsed = time.time() - start_time
        logger.warning(
            "Interactive agent timed out: job='%s', timeout=%ds",
            name, timeout_seconds,
        )
        return {
            "status": "error",
            "error": f"Interactive agent timed out after {timeout_seconds}s: {exc}",
            "elapsed_seconds": round(elapsed, 1),
        }
    except Exception as exc:
        elapsed = time.time() - start_time
        logger.error(
            "Interactive agent spawn failed: job='%s', error=%s",
            name, exc, exc_info=True,
        )
        return {
            "status": "error",
            "error": str(exc),
            "elapsed_seconds": round(elapsed, 1),
        }
    finally:
        # Always close the PTY — the session persists independently
        if pty_session is not None:
            try:
                pty_session.close(force=False)
            except Exception:
                pass


def _get_session_id_by_name(session_name: str) -> str | None:
    """Retrieve a session ID by scanning Claude's session files.

    Interactive sessions don't print their ID to the terminal, so
    we scan ``~/.claude/sessions/*.json`` for a matching session name.

    Each session file is named ``<pid>.json`` and contains at minimum
    ``sessionId`` and ``name`` fields.
    """
    import json as _json

    sessions_dir = Path.home() / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        logger.debug("Sessions directory not found: %s", sessions_dir)
        return None

    try:
        # Scan in reverse order (newest PIDs first — most likely match)
        for path in sorted(sessions_dir.glob("*.json"), reverse=True):
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
                if data.get("name") == session_name:
                    sid = data.get("sessionId") or data.get("session_id")
                    logger.debug(
                        "Found session by name '%s': %s (file=%s)",
                        session_name, sid, path.name,
                    )
                    return sid
            except (OSError, _json.JSONDecodeError):
                continue
    except Exception as exc:
        logger.debug("Failed to scan sessions: %s", exc)

    return None


def _register_spawn(result: SpawnResult) -> None:
    """Write a persistent spawn to the agent registry.

    Isolated in its own function so registry import failures don't
    break the executor.
    """
    try:
        from work_buddy.sidecar.dispatch.registry import register_agent
        register_agent(result)
    except Exception as exc:
        logger.warning("Failed to register agent spawn: %s", exc)


def _check_agent_spawn_consent() -> bool:
    """Check if agent spawning is consented.

    Uses the existing consent cache from ``work_buddy.consent``.
    Returns True only if a valid "always" or unexpired "temporary"
    grant exists for the ``sidecar:agent_spawn`` operation.
    """
    try:
        from work_buddy.consent import ConsentCache
        cache = ConsentCache()
        return cache.is_granted(AGENT_SPAWN_CONSENT_OP)
    except Exception as exc:
        logger.warning("Consent check failed: %s — defaulting to deny.", exc)
        return False
