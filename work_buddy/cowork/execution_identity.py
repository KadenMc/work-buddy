"""Server-owned identity shape for one generation-fenced Co-work driver."""

from __future__ import annotations

import re

_GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SESSION_SUFFIX = "-cowork"


def cowork_execution_session_id(generation: str) -> str:
    """Put generation entropy first so session-directory prefixes stay unique."""

    normalized = str(generation or "").strip()
    if not _GENERATION_RE.fullmatch(normalized):
        raise ValueError("Co-work generation is not a safe session identity")
    return f"{normalized}{_SESSION_SUFFIX}"


def cowork_generation_from_session(session_id: str | None) -> str | None:
    """Return the bound generation for a Co-work execution session."""

    normalized = str(session_id or "").strip()
    if not normalized.endswith(_SESSION_SUFFIX):
        return None
    generation = normalized[: -len(_SESSION_SUFFIX)]
    if not _GENERATION_RE.fullmatch(generation):
        return None
    return generation


__all__ = [
    "cowork_execution_session_id",
    "cowork_generation_from_session",
]
