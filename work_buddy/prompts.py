"""Prompt template store with Jinja2 defaults/overrides.

Templates live in ``prompts/defaults/`` (version-controlled) and can be
overridden by dropping a same-named file in ``prompts/overrides/`` (gitignored).
The override directory is checked first, so user files win.

Usage::

    from work_buddy.prompts import get_prompt

    text = get_prompt("classify_system")
    text = get_prompt("triage_recommend_system", lens="intent", data_type="chrome",
                      actions=["close", "group", "capture", "investigate"])
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULTS_DIR = _REPO_ROOT / "prompts" / "defaults"
_OVERRIDES_DIR = _REPO_ROOT / "prompts" / "overrides"

# Module-level singleton — created once, reused forever.
_env: Environment | None = None


def _get_env() -> Environment:
    """Return the cached Jinja2 environment."""
    global _env  # noqa: PLW0603
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader([str(_OVERRIDES_DIR), str(_DEFAULTS_DIR)]),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
    return _env


def get_prompt(name: str, **variables: object) -> str:
    """Render a prompt template by name.

    Parameters
    ----------
    name:
        Template filename without directory, e.g. ``"classify_system"``
        (the ``.j2`` extension is appended automatically).
    **variables:
        Jinja2 template variables.  Templates using ``StrictUndefined``
        will raise if a required variable is missing.
    """
    template = _get_env().get_template(f"{name}.j2")
    return template.render(**variables)


def list_templates() -> dict[str, str]:
    """Return ``{name: source}`` for every discoverable template.

    *source* is ``"override"`` if the file lives in ``prompts/overrides/``,
    ``"default"`` otherwise.
    """
    result: dict[str, str] = {}
    for directory, label in [(_DEFAULTS_DIR, "default"), (_OVERRIDES_DIR, "override")]:
        if directory.is_dir():
            for p in sorted(directory.glob("*.j2")):
                result[p.stem] = label  # override wins because it's second
    return result
