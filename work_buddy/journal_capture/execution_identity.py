"""Server-authored identities for least-authority Journal prompt workers."""

from __future__ import annotations

import re


_REQUEST_ID_RE = re.compile(r"^jpgr_[0-9a-f]{32}$")
_SESSION_PREFIX = "journal-prompt:"
_SMART_REQUEST_ID_RE = re.compile(r"^jspr_[0-9a-f]{32}$")
_SMART_SESSION_PREFIX = "journal-smart:"


def journal_prompt_generation_session_id(request_id: str) -> str:
    """Return the constrained hosted-agent session for one generation request."""

    normalized = str(request_id or "").strip()
    if not _REQUEST_ID_RE.fullmatch(normalized):
        raise ValueError("Invalid Journal prompt generation request identity")
    return f"{_SESSION_PREFIX}{normalized}"


def journal_prompt_request_from_session(session_id: str | None) -> str | None:
    """Recover the generation request bound to a valid worker session."""

    if not isinstance(session_id, str) or not session_id.startswith(_SESSION_PREFIX):
        return None
    request_id = session_id[len(_SESSION_PREFIX) :]
    return request_id if _REQUEST_ID_RE.fullmatch(request_id) else None


def journal_smart_processing_session_id(request_id: str) -> str:
    """Return the constrained hosted-agent session for one Smart attempt."""

    normalized = str(request_id or "").strip()
    if not _SMART_REQUEST_ID_RE.fullmatch(normalized):
        raise ValueError("Invalid Journal Smart processing request identity")
    return f"{_SMART_SESSION_PREFIX}{normalized}"


def journal_smart_request_from_session(session_id: str | None) -> str | None:
    """Recover the Smart request bound to a valid worker session."""

    if not isinstance(session_id, str) or not session_id.startswith(
        _SMART_SESSION_PREFIX
    ):
        return None
    request_id = session_id[len(_SMART_SESSION_PREFIX) :]
    return (
        request_id
        if _SMART_REQUEST_ID_RE.fullmatch(request_id)
        else None
    )


__all__ = [
    "journal_prompt_generation_session_id",
    "journal_prompt_request_from_session",
    "journal_smart_processing_session_id",
    "journal_smart_request_from_session",
]
