"""Discover + validate ``event_source`` ``.md`` files under
``.data/event_sources/``.

Loading is read-only and best-effort: an invalid source is skipped with a
logged reason and surfaced in the returned ``errors`` list (so the dashboard /
authoring loop can show *why*), never crashing the poll tick.
"""

from __future__ import annotations

from pathlib import Path

from work_buddy.events.sources.definition import (
    EventSourceDef,
    from_frontmatter,
    validate_source_fm,
)
from work_buddy.frontmatter import parse_frontmatter
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def sources_dir() -> Path:
    """The directory holding event-source ``.md`` files (created on resolve)."""
    from work_buddy.paths import resolve

    d = resolve("event_sources")
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_event_sources(
    directory: Path | None = None,
) -> tuple[list[EventSourceDef], list[dict]]:
    """Return ``(valid_defs, errors)``. ``errors`` is a list of
    ``{"file": <name>, "errors": [<msg>, ...]}``. Tests pass ``directory``
    explicitly (mirrors ``create_user_job_file``'s pure-function shape)."""
    d = directory if directory is not None else sources_dir()
    valid: list[EventSourceDef] = []
    errors: list[dict] = []
    if not d.exists():
        return valid, errors

    for path in sorted(d.glob("*.md")):
        fm, _ = parse_frontmatter(path)
        errs = validate_source_fm(path.stem, fm)
        if errs:
            errors.append({"file": path.name, "errors": errs})
            logger.warning("event source %s invalid: %s", path.name, "; ".join(errs))
            continue
        valid.append(from_frontmatter(path.stem, fm))
    return valid, errors


def write_event_source(
    target_dir: Path,
    name: str,
    fm: dict,
    *,
    overwrite: bool = False,
) -> dict:
    """Validate ``fm`` and write it as ``<target_dir>/<name>.md``.

    Mirrors ``create_user_job_file``: a pure function (explicit ``target_dir``,
    so tests need no path monkeypatching), refuses to overwrite an existing file
    unless ``overwrite=True``, and returns ``{"success": bool, ...}``. The next
    poll tick re-loads the directory, so a new source needs no hot-reload hook.
    """
    import yaml

    name = (name or "").strip()
    errors = validate_source_fm(name, fm)
    if errors:
        return {"success": False, "error": "; ".join(errors), "errors": errors}

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{name}.md"
    if target.exists() and not overwrite:
        return {
            "success": False,
            "error": f"event source {name!r} already exists at {target}.",
        }

    block = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()
    target.write_text(f"---\n{block}\n---\n", encoding="utf-8")
    return {
        "success": True,
        "name": name,
        "file_path": str(target),
        "message": (
            f"Event source {name!r} written. The poller re-loads sources each "
            "tick, so it activates on the next poll."
        ),
    }
