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
    allow_demotion: bool = True  # Allow interactive→headless demotion if interactive unavailable

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


def load_jobs(jobs_dir: Path) -> list[Job]:
    """Load all job ``.md`` files from a directory.

    Files without a ``schedule`` in their frontmatter are skipped.
    Parse errors are logged but don't stop the batch.

    Args:
        jobs_dir: Directory containing job ``.md`` files.

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
            job = _parse_job_file(md_path)
            if job is not None:
                jobs.append(job)
        except Exception as exc:
            logger.warning("Failed to parse job file %s: %s", md_path.name, exc)

    return jobs


def _parse_job_file(file_path: Path) -> Job | None:
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

    allow_demotion = _parse_bool(fm.get("allow_demotion", True))

    return Job(
        name=name,
        file_path=file_path,
        schedule=schedule.strip(),
        recurring=recurring,
        job_type=job_type,
        capability=fm.get("capability", ""),
        params=fm.get("params", {}) or {},
        workflow=fm.get("workflow", ""),
        prompt=body.strip(),
        description=body.strip(),
        enabled=enabled,
        spawn_mode=spawn_mode,
        allow_demotion=allow_demotion,
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
    return f"{job.name}:{job.schedule}:{job.job_type}:{job.capability}:{job.workflow}"
