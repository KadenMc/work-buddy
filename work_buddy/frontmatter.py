"""YAML frontmatter extraction utility for Obsidian markdown files.

Provides functions to parse YAML frontmatter from individual files and
scan directories of markdown files, filtering by frontmatter fields.
"""

from pathlib import Path
from typing import Any, Callable

import yaml


def parse_frontmatter(file_path: Path) -> tuple[dict, str]:
    """Parse YAML frontmatter and body from a markdown file.

    Frontmatter is delimited by ``---`` on its own line at the start of
    the file.  If the file has no frontmatter, returns an empty dict and
    the full file content as the body.

    Args:
        file_path: Path to the markdown file.

    Returns:
        A tuple of (frontmatter_dict, body_string).
        On malformed YAML the dict is empty; the body is always returned.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return {}, ""

    if not text.startswith("---"):
        return {}, text

    # Find the closing delimiter
    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        return {}, text

    yaml_block = text[3:end_idx].strip()
    body = text[end_idx + 4:].lstrip("\n")

    if not yaml_block:
        return {}, body

    try:
        fm = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return {}, body

    if not isinstance(fm, dict):
        return {}, body

    return fm, body


def scan_frontmatter(
    directory: Path,
    recursive: bool = True,
    filter_fn: Callable[[dict], bool] | None = None,
) -> list[dict]:
    """Scan a directory for .md files and extract frontmatter from each.

    Args:
        directory: Root directory to scan.
        recursive: If True, scan subdirectories as well.
        filter_fn: Optional predicate applied to the frontmatter dict.
                   Only entries where ``filter_fn(frontmatter)`` is True
                   are included.

    Returns:
        A list of dicts, each containing:
        - ``path``: the file's ``Path``
        - ``frontmatter``: the parsed frontmatter dict
        - all top-level frontmatter keys are also merged in for convenience
    """
    pattern = "**/*.md" if recursive else "*.md"
    results: list[dict] = []

    for md_path in sorted(directory.glob(pattern)):
        if not md_path.is_file():
            continue

        fm, _ = parse_frontmatter(md_path)

        if filter_fn is not None and not filter_fn(fm):
            continue

        entry: dict[str, Any] = {"path": md_path, "frontmatter": fm}
        entry.update(fm)
        results.append(entry)

    return results


def filter_by_field(entries: list[dict], field: str, value: Any) -> list[dict]:
    """Filter scan results by a specific frontmatter field value.

    Args:
        entries: Output from :func:`scan_frontmatter`.
        field: The frontmatter key to match on.
        value: The value to match (exact equality).

    Returns:
        Filtered list of entries.
    """
    return [e for e in entries if e.get("frontmatter", {}).get(field) == value]


def filter_by_status(entries: list[dict], status: str) -> list[dict]:
    """Convenience wrapper: filter by the ``status`` frontmatter field.

    Args:
        entries: Output from :func:`scan_frontmatter`.
        status: Status value to match (e.g. ``"active"``).

    Returns:
        Filtered list of entries.
    """
    return filter_by_field(entries, "status", status)
