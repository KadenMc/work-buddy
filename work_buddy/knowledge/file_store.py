"""File-per-unit storage layer for the system knowledge store.

Each knowledge unit is one Markdown file: YAML frontmatter (every structured
field) + a Markdown body (``content.full``). A unit at store path ``P`` lives
at ``knowledge/store/<P>.md``; the path↔file mapping is bijective. A parent
unit and its children's directory coexist (``tasks.md`` beside ``tasks/``) —
no ``index.md`` convention.

This module is the read/write **seam** between the knowledge-store engine and
the on-disk files — ``read_unit`` / ``write_unit`` / ``list_unit_paths`` /
``delete_unit`` / ``move_unit``, plus the bulk ``load_units_from_dir``. A future
storage provider (SQLite, a remote store) can implement the same seam without
touching the engine.

The codec round-trips a raw unit dict — the shape ``model.unit_from_dict``
consumes — to and from the file format. Prose-kind units and workflow units
have different body layouts: a workflow unit carries its ``steps`` DAG in
frontmatter and its per-step instructions as ``## <step-id>`` body sections,
so the codec dispatches on ``kind``.

``children`` is never stored — it is derived at load time from other units'
``parents`` (see ``load_units_from_dir``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Field-ordering policy
# ---------------------------------------------------------------------------
#
# Frontmatter keys are written in a stable order so a no-op edit is a no-op
# diff. ``content`` (→ body), ``children`` (derived), ``path`` (→ filename)
# and ``scope`` (loader-populated) never appear in frontmatter.

_FRONT_HEAD = ("name", "kind", "description", "summary")
_FRONT_TAIL = ("tags", "aliases", "parents", "requires", "dev_notes")
_FRONT_DROP = ("content", "children", "path", "scope")

# Workflow units render ``step_instructions`` as body sections, not frontmatter.
_WORKFLOW_BODY_FIELDS = ("step_instructions",)


# ---------------------------------------------------------------------------
# YAML serialization — block style for multi-line strings
# ---------------------------------------------------------------------------

class _StoreYamlDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings as literal ``|`` blocks."""


def _str_representer(dumper: yaml.Dumper, data: str):  # type: ignore[override]
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_StoreYamlDumper.add_representer(str, _str_representer)


def _dump_frontmatter(fields: dict[str, Any]) -> str:
    """Serialize a frontmatter dict to YAML, preserving the given key order."""
    return yaml.dump(
        fields,
        Dumper=_StoreYamlDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=4096,
    )


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a unit file into (frontmatter dict, body).

    Raises ``ValueError`` on a missing or malformed frontmatter block so a
    corrupt file fails loud rather than loading as an empty unit.
    """
    if not text.startswith("---"):
        raise ValueError(
            "unit file has no YAML frontmatter (expected a '---' block at the top)"
        )
    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        raise ValueError("frontmatter is not closed (no terminating '---' line)")

    yaml_block = text[3:end_idx].strip()
    body = text[end_idx + 4:].lstrip("\n")

    if not yaml_block:
        return {}, body
    try:
        fm = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(fm, dict):
        raise ValueError("YAML frontmatter must be a mapping of fields")
    return fm, body


# ---------------------------------------------------------------------------
# Frontmatter ordering
# ---------------------------------------------------------------------------

def _ordered_frontmatter(
    unit_dict: dict[str, Any],
    *,
    summary: Any = None,
    extra_drop: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build an order-stable frontmatter dict from a raw unit dict.

    Head fields first (name, kind, description, summary), then kind-specific
    fields in their incoming order, then the shared tail (tags, aliases,
    parents, requires, dev_notes). ``content`` / ``children`` / ``path`` /
    ``scope`` and any ``extra_drop`` keys are excluded.
    """
    drop = set(_FRONT_DROP) | set(extra_drop)
    fields: dict[str, Any] = {}

    for key in _FRONT_HEAD:
        if key == "summary":
            if summary is not None:
                fields["summary"] = summary
        elif unit_dict.get(key) not in (None, "", [], {}):
            fields[key] = unit_dict[key]

    known = set(_FRONT_HEAD) | set(_FRONT_TAIL) | drop
    for key, value in unit_dict.items():
        if key in known:
            continue
        if value in (None, "", [], {}):
            continue
        fields[key] = value

    for key in _FRONT_TAIL:
        value = unit_dict.get(key)
        if value in (None, "", [], {}):
            continue
        fields[key] = value

    return fields


# ---------------------------------------------------------------------------
# Serialize — unit dict → markdown
# ---------------------------------------------------------------------------

def unit_dict_to_markdown(unit_dict: dict[str, Any]) -> str:
    """Serialize a raw unit dict to a YAML-frontmatter + Markdown file.

    Dispatches on ``kind``: workflow units use the ``## <step-id>`` body
    layout, every other kind uses the plain prose layout.
    """
    if unit_dict.get("kind") == "workflow":
        return _workflow_to_markdown(unit_dict)
    return _prose_to_markdown(unit_dict)


def _content_parts(unit_dict: dict[str, Any]) -> tuple[str, Any]:
    """Return (body, summary) from a unit dict's ``content`` map."""
    content = unit_dict.get("content") or {}
    if not isinstance(content, dict):
        return "", None
    return content.get("full", "") or "", content.get("summary")


def _render(fields: dict[str, Any], body: str) -> str:
    """Assemble a frontmatter dict and body into a unit file's text."""
    frontmatter = _dump_frontmatter(fields)
    if body:
        return f"---\n{frontmatter}---\n\n{body.rstrip(chr(10))}\n"
    return f"---\n{frontmatter}---\n"


def _prose_to_markdown(unit_dict: dict[str, Any]) -> str:
    body, summary = _content_parts(unit_dict)
    fields = _ordered_frontmatter(unit_dict, summary=summary)
    return _render(fields, body)


def _workflow_to_markdown(unit_dict: dict[str, Any]) -> str:
    """Serialize a workflow unit: ``steps`` in frontmatter, ``step_instructions``
    as ``## <step-id>`` body sections after the workflow-level narrative."""
    body, summary = _content_parts(unit_dict)
    fields = _ordered_frontmatter(
        unit_dict, summary=summary, extra_drop=_WORKFLOW_BODY_FIELDS,
    )

    step_instructions = unit_dict.get("step_instructions") or {}
    steps = unit_dict.get("steps") or []

    # Section order: declared step order first, then any orphan instruction
    # keys (a step id that is not in ``steps``) so no data is silently lost on
    # write — the parser/validator surfaces the orphan.
    ordered_ids: list[str] = []
    for step in steps:
        sid = step.get("id") if isinstance(step, dict) else None
        if sid and sid in step_instructions and sid not in ordered_ids:
            ordered_ids.append(sid)
    for sid in step_instructions:
        if sid not in ordered_ids:
            ordered_ids.append(sid)

    sections = [body.rstrip("\n")] if body else []
    for sid in ordered_ids:
        text = (step_instructions.get(sid) or "").rstrip("\n")
        sections.append(f"## {sid}\n\n{text}" if text else f"## {sid}")

    full_body = "\n\n".join(s for s in sections if s)
    return _render(fields, full_body)


# ---------------------------------------------------------------------------
# Parse — markdown → unit dict
# ---------------------------------------------------------------------------

def markdown_to_unit_dict(text: str) -> dict[str, Any]:
    """Parse a unit file back into a raw unit dict.

    Inverse of :func:`unit_dict_to_markdown`. Dispatches on the frontmatter
    ``kind``: workflow units reassemble ``step_instructions`` from their
    ``## <step-id>`` body sections.
    """
    fm, body = _split_frontmatter(text)
    fm.pop("path", None)  # informational only if present in a hand-edited file
    if fm.get("kind") == "workflow":
        return _workflow_from_markdown(fm, body)
    return _prose_from_markdown(fm, body)


def _fold_content(fm: dict[str, Any], body: str) -> dict[str, Any]:
    """Fold ``summary`` (frontmatter) and ``body`` back into a ``content`` map."""
    summary = fm.pop("summary", None)
    unit_dict: dict[str, Any] = dict(fm)
    content: dict[str, Any] = {}
    if summary is not None:
        content["summary"] = summary
    if body.strip():
        content["full"] = body.rstrip("\n")
    if content:
        unit_dict["content"] = content
    return unit_dict


def _prose_from_markdown(fm: dict[str, Any], body: str) -> dict[str, Any]:
    return _fold_content(fm, body)


def _workflow_from_markdown(fm: dict[str, Any], body: str) -> dict[str, Any]:
    """Parse a workflow unit, splitting the body into ``content.full`` and
    ``## <step-id>`` instruction sections.

    Per the workflow-format parser rule: ``steps`` is read from frontmatter
    first, so the exact set of step ids is known. The body is split into
    sections only at ``## <text>`` lines where ``<text>`` exactly equals a
    known step id; any other ``## `` line is ordinary content within whatever
    section it falls in. Body text before the first step-id heading is
    ``content.full``.
    """
    steps = fm.get("steps") or []
    step_ids = {
        step["id"] for step in steps
        if isinstance(step, dict) and step.get("id")
    }

    lines = body.split("\n")
    narrative_lines: list[str] = []
    current_id: str | None = None
    sections: dict[str, list[str]] = {}

    for line in lines:
        heading_id: str | None = None
        if line.startswith("## "):
            candidate = line[3:].strip()
            if candidate in step_ids:
                heading_id = candidate
        if heading_id is not None:
            current_id = heading_id
            sections.setdefault(current_id, [])
            continue
        if current_id is None:
            narrative_lines.append(line)
        else:
            sections[current_id].append(line)

    step_instructions = {
        sid: "\n".join(text_lines).strip("\n")
        for sid, text_lines in sections.items()
    }
    narrative = "\n".join(narrative_lines).strip("\n")

    unit_dict = _fold_content(fm, narrative)
    if step_instructions:
        unit_dict["step_instructions"] = step_instructions
    return unit_dict


# ---------------------------------------------------------------------------
# Path ↔ file mapping
# ---------------------------------------------------------------------------

def path_to_file(store_dir: Path, unit_path: str) -> Path:
    """Map a unit store path to its ``.md`` file under ``store_dir``."""
    return store_dir / f"{unit_path}.md"


def file_to_path(store_dir: Path, file_path: Path) -> str:
    """Map a ``.md`` file under ``store_dir`` back to its unit store path."""
    rel = file_path.resolve().relative_to(store_dir.resolve())
    return rel.with_suffix("").as_posix()


# ---------------------------------------------------------------------------
# Read/write seam
# ---------------------------------------------------------------------------

def read_unit(store_dir: Path, unit_path: str) -> dict[str, Any] | None:
    """Read one unit's raw dict from its file, or None if the file is absent."""
    file_path = path_to_file(store_dir, unit_path)
    if not file_path.is_file():
        return None
    return markdown_to_unit_dict(file_path.read_text(encoding="utf-8"))


def write_unit(store_dir: Path, unit_path: str, unit_dict: dict[str, Any]) -> Path:
    """Write one unit's raw dict to its ``.md`` file, creating parent dirs."""
    file_path = path_to_file(store_dir, unit_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(unit_dict_to_markdown(unit_dict), encoding="utf-8")
    return file_path


def delete_unit(store_dir: Path, unit_path: str) -> bool:
    """Delete one unit's file. Returns True if a file was removed."""
    file_path = path_to_file(store_dir, unit_path)
    if not file_path.is_file():
        return False
    file_path.unlink()
    _prune_empty_dirs(store_dir, file_path.parent)
    return True


def move_unit(store_dir: Path, old_path: str, new_path: str) -> None:
    """Move a unit file from ``old_path`` to ``new_path``."""
    src = path_to_file(store_dir, old_path)
    dst = path_to_file(store_dir, new_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    _prune_empty_dirs(store_dir, src.parent)


def list_unit_paths(store_dir: Path) -> list[str]:
    """Return every unit store path with a ``.md`` file under ``store_dir``."""
    if not store_dir.is_dir():
        return []
    return sorted(
        file_to_path(store_dir, f)
        for f in store_dir.rglob("*.md")
        if f.is_file()
    )


def _prune_empty_dirs(store_dir: Path, directory: Path) -> None:
    """Remove now-empty directories up to (but not including) ``store_dir``."""
    store_dir = store_dir.resolve()
    current = directory.resolve()
    while current != store_dir and current.is_dir() and not any(current.iterdir()):
        parent = current.parent
        current.rmdir()
        current = parent


# ---------------------------------------------------------------------------
# Bulk load
# ---------------------------------------------------------------------------

def load_units_from_dir(store_dir: Path) -> dict[str, dict[str, Any]]:
    """Load every ``.md`` unit under ``store_dir`` into a {path: raw dict} map.

    ``children`` is derived here, not read from files: a unit's children are
    every unit that names it in ``parents``. Each returned dict is the raw
    shape ``model.unit_from_dict`` consumes.
    """
    from work_buddy.logging_config import get_logger

    logger = get_logger(__name__)
    units: dict[str, dict[str, Any]] = {}

    for file_path in sorted(store_dir.rglob("*.md")):
        if not file_path.is_file():
            continue
        unit_path = file_to_path(store_dir, file_path)
        try:
            units[unit_path] = markdown_to_unit_dict(
                file_path.read_text(encoding="utf-8")
            )
        except (ValueError, OSError) as exc:
            logger.warning("Failed to load unit file %s: %s", file_path, exc)

    _derive_children(units)
    return units


def _derive_children(units: dict[str, dict[str, Any]]) -> None:
    """Populate each unit's ``children`` from other units' ``parents``."""
    children: dict[str, list[str]] = {}
    for path, unit in units.items():
        for parent in unit.get("parents", []) or []:
            children.setdefault(parent, []).append(path)
    for path, unit in units.items():
        kids = children.get(path)
        if kids:
            unit["children"] = sorted(kids)
        else:
            unit.pop("children", None)
