"""Project-note markdown format — parse and render.

A project note is the markdown surface for one project in the
markdown-canonical model (see ``architecture/markdown-db``). It lives at
``work/projects/<slug>/<slug>.md`` in the vault and has the shape::

    ---
    slug: ecg-fm
    name: ECG Foundation Model
    status: active
    ---
    # ECG Foundation Model

    Free-form description body. This is the long-form prose the
    original version-control task (t-98d34cf6) worried about losing —
    now a first-class, git-syncable, hand-editable file.

YAML frontmatter carries the typed identity fields (``slug`` / ``name``
/ ``status``); the body carries the ``description``. The leading
``# <name>`` H1 is cosmetic — it is rendered on write and stripped on
read so it never leaks into the description field.

Stdlib + PyYAML only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<fm>.*?)\r?\n---[ \t]*\r?\n?(?P<body>.*)\Z",
    re.DOTALL,
)


class ProjectNoteParseError(ValueError):
    """A file under work/projects/ is not a well-formed project note."""


@dataclass
class ProjectNote:
    """The parsed content of one project note."""

    slug: str
    name: str
    status: str
    description: str


def parse_project_note(text: str) -> ProjectNote:
    """Parse project-note markdown into a :class:`ProjectNote`.

    Raises :class:`ProjectNoteParseError` when the frontmatter is
    missing, unparseable, or lacks a ``slug``. A missing ``name`` falls
    back to the slug; a missing ``status`` falls back to ``active``.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ProjectNoteParseError("missing or malformed YAML frontmatter")
    try:
        fm = yaml.safe_load(m.group("fm")) or {}
    except yaml.YAMLError as exc:
        raise ProjectNoteParseError(f"frontmatter is not valid YAML: {exc}")
    if not isinstance(fm, dict):
        raise ProjectNoteParseError("frontmatter did not parse to a mapping")

    slug = fm.get("slug")
    if not slug or not isinstance(slug, str):
        raise ProjectNoteParseError("frontmatter is missing a 'slug'")

    name = fm.get("name") or slug
    status = fm.get("status") or "active"

    body = m.group("body")
    description = _strip_leading_h1(body).strip()

    return ProjectNote(
        slug=str(slug).strip(),
        name=str(name).strip(),
        status=str(status).strip(),
        description=description,
    )


def render_project_note(
    slug: str, name: str | None, status: str, description: str | None,
) -> str:
    """Render a :class:`ProjectNote`'s fields back to markdown.

    The inverse of :func:`parse_project_note` — round-tripping a parsed
    note through render produces equivalent content (modulo whitespace
    normalization of the body).
    """
    display_name = (name or slug).strip()
    fm: dict[str, Any] = {
        "slug": slug,
        "name": display_name,
        "status": status,
    }
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    body = (description or "").strip()
    return f"---\n{fm_text}\n---\n# {display_name}\n\n{body}\n"


def _strip_leading_h1(body: str) -> str:
    """Drop a single leading ``# ...`` heading line from a note body.

    Only the first non-blank line is considered; if it is an H1 it is
    the rendered name heading and not part of the description.
    """
    lines = body.splitlines()
    out: list[str] = []
    dropped = False
    for line in lines:
        if not dropped:
            if line.strip() == "":
                continue  # skip blank lines before the heading
            if line.lstrip().startswith("# "):
                dropped = True
                continue  # drop the H1
            # First content line is not an H1 — keep everything as-is.
            return body
        out.append(line)
    return "\n".join(out)
