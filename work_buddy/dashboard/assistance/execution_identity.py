"""Server-authored identities for least-authority hosted form agents."""

from __future__ import annotations

import re

_GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SUFFIX = "-assisted-draft"


def assistance_execution_session_id(generation: str) -> str:
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
        raise ValueError("Invalid assistance execution generation")
    return f"{generation}{_SUFFIX}"


def assistance_generation_from_session(session_id: str | None) -> str | None:
    if not isinstance(session_id, str) or not session_id.endswith(_SUFFIX):
        return None
    generation = session_id[: -len(_SUFFIX)]
    return generation if _GENERATION.fullmatch(generation) else None
