"""Job loading and management.

Jobs are ``.md`` files in ``sidecar_jobs/`` with YAML frontmatter
defining schedule, type, and execution parameters.

Three job types:
  - **capability**: Execute a registered MCP gateway capability.
  - **workflow**: Execute a registered workflow by name.
  - **prompt**: Freeform prompt text (future: agent execution).

Adapted from ClaudeClaw's jobs.ts but extended for our multi-type model.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from work_buddy.frontmatter import parse_frontmatter
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class Job:
    """A schedulable unit of work."""

    name: str  # Derived from filename (without .md)
    file_path: Path
    schedule: str  # 5-field cron expression
    recurring: bool = True

    # Origin of the job file: "system" (ships with work-buddy, git-tracked
    # under sidecar_jobs/) or "user" (authored locally, gitignored under
    # <data_root>/user_jobs/). Used by the dashboard to group jobs and
    # by the loader to decide collision policy (user overrides system).
    source: str = "system"

    # Job type determines execution path
    job_type: str = "prompt"  # capability | workflow | prompt

    # For type=capability
    capability: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    # For type=workflow
    workflow: str = ""

    # For type=prompt (and fallback description)
    prompt: str = ""  # Body text of the .md file
    description: str = ""

    # Execution control
    enabled: bool = True

    # Agent spawn control (for type=prompt and workflow reasoning steps)
    spawn_mode: str = ""  # headless_ephemeral | headless_persistent | interactive_persistent

    # Runtime state (not persisted in .md frontmatter — populated by scheduler)
    last_run_at: float = 0.0
    last_result: str = ""  # ok | error
    last_error: str = ""  # human-readable error reason (set on failure)


def _parse_bool(value: Any) -> bool:
    """Parse a boolean from various YAML representations."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def load_jobs(jobs_dir: Path, source: str = "system") -> list[Job]:
    """Load all job ``.md`` files from a directory.

    Files without a ``schedule`` in their frontmatter are skipped.
    Parse errors are logged but don't stop the batch.

    Args:
        jobs_dir: Directory containing job ``.md`` files.
        source: Origin label stamped on every loaded job
            (``"system"`` or ``"user"``).

    Returns:
        List of parsed Job objects.
    """
    if not jobs_dir.is_dir():
        return []

    jobs: list[Job] = []
    for md_path in sorted(jobs_dir.glob("*.md")):
        if not md_path.is_file():
            continue

        try:
            job = _parse_job_file(md_path, source=source)
            if job is not None:
                jobs.append(job)
        except Exception as exc:
            logger.warning("Failed to parse job file %s: %s", md_path.name, exc)

    return jobs


def load_jobs_from_many(dirs: list[tuple[Path, str]]) -> list[Job]:
    """Load jobs from a list of ``(directory, source)`` pairs and merge.

    Later entries override earlier ones on filename-stem collisions.
    A collision logs a WARN naming both the loser and the winner so
    surprised users can see which file is in effect. Concretely, the
    intended ordering is ``[(system_dir, "system"), (user_dir, "user")]``,
    so a user file with the same stem as a system file wins and the
    system file is dropped.

    Args:
        dirs: Ordered list of ``(jobs_dir, source_label)`` pairs.

    Returns:
        Merged list of Job objects, deduplicated by ``Job.name``.
    """
    by_name: dict[str, Job] = {}
    for jobs_dir, source in dirs:
        for job in load_jobs(jobs_dir, source=source):
            existing = by_name.get(job.name)
            if existing is not None:
                logger.warning(
                    "Job name collision on %r: %s job at %s overrides "
                    "%s job at %s.",
                    job.name, job.source, job.file_path,
                    existing.source, existing.file_path,
                )
            by_name[job.name] = job
    return list(by_name.values())


def _parse_job_file(file_path: Path, source: str = "system") -> Job | None:
    """Parse a single job file.

    Returns None if the file has no schedule (not a valid job).
    """
    fm, body = parse_frontmatter(file_path)

    schedule = fm.get("schedule", "")
    if not schedule or not isinstance(schedule, str):
        return None

    name = file_path.stem

    # Determine job type
    job_type = fm.get("type", "prompt")
    if job_type not in ("capability", "workflow", "prompt"):
        logger.warning("Unknown job type '%s' in %s — defaulting to prompt.", job_type, name)
        job_type = "prompt"

    # Recurring: default True, supports legacy "daily" alias
    recurring = _parse_bool(fm.get("recurring", fm.get("daily", True)))

    # Enabled: default True
    enabled = _parse_bool(fm.get("enabled", True))

    # Spawn mode: validated but stored as string (executor resolves to SpawnMode)
    spawn_mode = fm.get("spawn_mode", "")
    if spawn_mode and spawn_mode not in (
        "headless_ephemeral", "headless_persistent", "interactive_persistent",
    ):
        logger.warning(
            "Unknown spawn_mode '%s' in %s — ignoring (will use default).",
            spawn_mode, name,
        )
        spawn_mode = ""

    return Job(
        name=name,
        file_path=file_path,
        schedule=schedule.strip(),
        recurring=recurring,
        source=source,
        job_type=job_type,
        capability=fm.get("capability", ""),
        params=fm.get("params", {}) or {},
        workflow=fm.get("workflow", ""),
        prompt=body.strip(),
        description=body.strip(),
        enabled=enabled,
        spawn_mode=spawn_mode,
    )


def clear_job_schedule(job: Job) -> None:
    """Remove the ``schedule:`` line from a one-shot job's frontmatter.

    This is how one-shot jobs self-expire after firing once:
    the schedule is removed so they won't match on the next tick.
    """
    try:
        text = job.file_path.read_text(encoding="utf-8")
    except OSError:
        return

    lines = text.split("\n")
    new_lines = [
        line for line in lines
        if not line.strip().startswith("schedule:")
    ]
    job.file_path.write_text("\n".join(new_lines), encoding="utf-8")
    logger.info("Cleared schedule for one-shot job: %s", job.name)


def job_fingerprint(job: Job) -> str:
    """Generate a fingerprint for change detection during hot-reload."""
    return (
        f"{job.name}:{job.schedule}:{job.job_type}:"
        f"{job.capability}:{job.workflow}:{job.source}"
    )


_NAME_PATTERN = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _registry_names(kind: str) -> list[str]:
    """Return registered names for ``kind`` ('capability' or 'workflow').

    Lazy import — avoids a hard sidecar→mcp_server dependency at module
    load. Returns an empty list on any registry-fetch failure rather
    than blocking job creation; in that case the caller's name passes
    through unchecked (degrades to the previous lenient behavior).
    """
    try:
        from work_buddy.mcp_server.registry import (
            get_registry, Capability, WorkflowDefinition,
        )
        reg = get_registry()
        if kind == "capability":
            return [n for n, e in reg.items() if isinstance(e, Capability)]
        return [n for n, e in reg.items() if isinstance(e, WorkflowDefinition)]
    except Exception:
        return []


def _slash_command_to_registry(provided: str, kind: str) -> str | None:
    """If ``provided`` looks like a slash-command name (e.g. ``wb-morning``
    or ``morning`` where a slash command ``wb-morning`` exists), return
    the underlying registry entry name (e.g. ``morning-routine``).
    """
    try:
        from work_buddy.mcp_server.registry import (
            get_registry, Capability, WorkflowDefinition,
        )
        reg = get_registry()
        # The "stem" we're looking for: try with and without the wb- prefix.
        stems = [provided, f"wb-{provided}"]
        if provided.startswith("wb-"):
            stems.append(provided[3:])
        type_filter = (Capability if kind == "capability"
                       else WorkflowDefinition)
        for stem in stems:
            for name, entry in reg.items():
                if not isinstance(entry, type_filter):
                    continue
                if (getattr(entry, "slash_command", None) or "") == stem:
                    return name
    except Exception:
        return None
    return None


def _suggest_close_name(provided: str, choices: list[str], n: int = 3) -> list[str]:
    """Return up to ``n`` closest matches for ``provided`` from ``choices``."""
    import difflib
    return difflib.get_close_matches(provided, choices, n=n, cutoff=0.4)


def _validate_registry_name(kind: str, provided: str) -> dict | None:
    """Return None if ``provided`` is a registered name for ``kind``,
    else a typed error dict ready to return from create_user_job_file.

    The error prioritizes a slash-command-to-registry match if one
    exists — users (and agents) often remember the user-facing
    ``wb-morning`` slash command rather than its underlying
    ``morning-routine`` workflow.
    """
    choices = _registry_names(kind)
    if not choices:
        # Couldn't reach the registry — don't block creation. Caller
        # will hit a clearer error at fire time.
        return None
    if provided in choices:
        return None
    # Slash-command-aware suggestion takes priority over fuzzy match.
    slash_resolved = _slash_command_to_registry(provided, kind)
    if slash_resolved:
        msg = (
            f"Unknown {kind} {provided!r} — did you mean "
            f"``{slash_resolved}``? (``{provided}`` is the slash-command "
            f"name; the underlying {kind} is ``{slash_resolved}``.)"
        )
        return {
            "success": False,
            "error": msg,
            "errors_by_field": {kind: msg},
            "suggestions": [slash_resolved],
        }
    suggestions = _suggest_close_name(provided, choices)
    msg = f"Unknown {kind} {provided!r}."
    if suggestions:
        quoted = ", ".join(repr(s) for s in suggestions)
        msg += f" Did you mean: {quoted}?"
    return {
        "success": False,
        "error": msg,
        "errors_by_field": {kind: msg},
        "suggestions": suggestions,
    }


def _validate_workflow_params(workflow_name: str, params: dict) -> dict | None:
    """Pre-validate workflow params against the workflow's declared
    ``params_schema`` (if any). Mirrors the conductor's start-time
    validation but at job-create time so typos surface immediately
    instead of on first fire.
    """
    try:
        from work_buddy.mcp_server.registry import (
            get_registry, WorkflowDefinition,
        )
        reg = get_registry()
        wf = reg.get(workflow_name)
        if not isinstance(wf, WorkflowDefinition):
            return None  # Already caught by name validation above.
    except Exception:
        return None
    schema = getattr(wf, "params_schema", None) or {}
    if not schema and params:
        return {
            "success": False,
            "error": (
                f"Workflow {workflow_name!r} does not declare a params schema "
                f"but params were provided: {sorted(params.keys())}."
            ),
            "errors_by_field": {"params": "this workflow accepts no params"},
        }
    if not schema:
        return None
    declared = set(schema.keys())
    provided = set(params.keys())
    unknown = provided - declared
    required = {k for k, v in schema.items() if isinstance(v, dict) and v.get("required")}
    missing = required - provided
    errors = []
    if unknown:
        errors.append(f"unknown param(s): {sorted(unknown)}")
    if missing:
        errors.append(f"missing required param(s): {sorted(missing)}")
    if not errors:
        return None
    return {
        "success": False,
        "error": (
            f"Params validation failed for workflow {workflow_name!r}: "
            + "; ".join(errors)
        ),
        "errors_by_field": {"params": "; ".join(errors)},
    }


def create_user_job_file(
    target_dir: Path,
    *,
    name: str,
    schedule: str,
    job_type: str = "prompt",
    capability: str = "",
    params: dict | None = None,
    workflow: str = "",
    prompt: str = "",
    enabled: bool = True,
    recurring: bool = True,
    overwrite: bool = False,
) -> dict:
    """Validate inputs and write a user job .md file.

    Pure function — takes the destination directory explicitly so tests can
    point it at a temp dir without monkeypatching paths.data_dir. Refuses to
    overwrite an existing file unless ``overwrite=True`` (used by the
    Edit-job flow, which needs to replace an existing file in place).
    Returns ``{"success": bool, ...}``.
    """
    import json as _json

    from work_buddy.sidecar.scheduler.cron import parse_cron_field

    name = (name or "").strip()
    if not name:
        return {"success": False, "error": "name is required."}
    if not _NAME_PATTERN.fullmatch(name):
        return {
            "success": False,
            "error": (
                "name must be 1-64 chars, start alphanumeric, and contain "
                "only letters, digits, hyphens, or underscores."
            ),
        }

    schedule = (schedule or "").strip()
    fields = schedule.split()
    if len(fields) != 5:
        return {
            "success": False,
            "error": "schedule must be a 5-field cron expression "
                     "(MIN HOUR DOM MON DOW).",
        }
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    for i, (field, (lo, hi)) in enumerate(zip(fields, ranges)):
        if not parse_cron_field(field, lo, hi):
            return {
                "success": False,
                "error": f"schedule field #{i+1} ({field!r}) is invalid.",
            }

    if job_type not in ("capability", "workflow", "prompt"):
        return {
            "success": False,
            "error": f"job_type must be capability|workflow|prompt, got {job_type!r}.",
        }
    # Normalize a leading slash and surrounding whitespace from the
    # capability/workflow names. Users and agents often paste the
    # slash-command form they remember (``/wb-morning``); without
    # this strip, the validator sees "/wb-morning" and rejects it
    # without recognizing the slash-command stem behind it.
    capability = (capability or "").strip().lstrip("/")
    workflow = (workflow or "").strip().lstrip("/")

    if job_type == "capability" and not capability:
        return {"success": False, "error": "type=capability requires 'capability'."}
    if job_type == "workflow" and not workflow:
        return {"success": False, "error": "type=workflow requires 'workflow'."}
    if job_type == "prompt" and not (prompt or "").strip():
        return {"success": False, "error": "type=prompt requires 'prompt'."}

    # Registry-backed name validation. Hallucinated names (agent confuses
    # the slash-command ``/wb-morning`` with a workflow named "morning",
    # or types ``task_create`` instead of ``task_create_simple``, etc.)
    # used to write a syntactically-valid file that silently failed at
    # fire time — possibly days later for a weekly cron. Validate now;
    # surface a typed error with the closest match so the caller (form
    # or chat agent) can correct the input immediately.
    if job_type in ("capability", "workflow"):
        provided = capability if job_type == "capability" else workflow
        registry_err = _validate_registry_name(job_type, provided)
        if registry_err is not None:
            return registry_err
        # For workflows, also pre-validate params against any declared
        # params_schema so a typo'd param key isn't caught only at fire
        # time. Capabilities don't have an introspection-time schema we
        # can validate against here without dragging in heavy imports;
        # the executor will surface those at fire time.
        if job_type == "workflow" and params is not None:
            params_err = _validate_workflow_params(provided, params)
            if params_err is not None:
                return params_err

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{name}.md"
    if target.exists() and not overwrite:
        return {
            "success": False,
            "error": f"User job {name!r} already exists at {target}.",
        }

    fm_lines = [
        "---",
        f'schedule: "{schedule}"',
        f"type: {job_type}",
        f"recurring: {str(bool(recurring)).lower()}",
        f"enabled: {str(bool(enabled)).lower()}",
    ]
    if job_type == "capability":
        fm_lines.append(f"capability: {capability.strip()}")
    elif job_type == "workflow":
        fm_lines.append(f"workflow: {workflow.strip()}")
    if job_type in ("capability", "workflow") and params:
        fm_lines.append(f"params: {_json.dumps(params)}")
    fm_lines.append("---")

    body = prompt if job_type == "prompt" else ""
    target.write_text("\n".join(fm_lines) + "\n\n" + body.strip() + "\n", encoding="utf-8")

    return {
        "success": True,
        "name": name,
        "file_path": str(target),
        "message": (
            f"User job {name!r} created. Scheduler will pick it up on the "
            "next hot-reload (~30s)."
        ),
    }
