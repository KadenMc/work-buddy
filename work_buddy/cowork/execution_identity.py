"""Server-owned identity shapes for constrained Co-work executions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SESSION_SUFFIX = "-cowork"
_VERIFY_JOB_MARKER = "-cowork-verify-"
_TRUTH_ANALYSIS_SUFFIX = "-cowork-truth-analysis"


class CoworkVerifyRole(str, Enum):
    """The four independently authorized hosted roles in a Verify run."""

    SPECIALIST = "specialist"
    REVISER = "reviser"
    COORDINATOR = "coordinator"
    COTHINK = "cothink"


@dataclass(frozen=True, slots=True)
class CoworkVerifyJobIdentity:
    """Identity recovered from a server-authored Verify worker session."""

    job_id: str
    role: CoworkVerifyRole


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


def cowork_verify_job_session_id(
    job_id: str,
    role: CoworkVerifyRole | str,
) -> str:
    """Return a role- and job-scoped hosted-agent session identity.

    The entropy-bearing job id leads the value for the same reason the
    persistent document-agent generation does: session-directory prefixes
    remain unique.  The role suffix is intentionally *not* ``-cowork``, so a
    Verify worker can never be mistaken for the broader persistent document
    agent by :func:`cowork_generation_from_session`.
    """

    normalized_job_id = str(job_id or "").strip()
    if not _GENERATION_RE.fullmatch(normalized_job_id):
        raise ValueError("Co-work Verify job id is not a safe session identity")
    try:
        normalized_role = (
            role if isinstance(role, CoworkVerifyRole) else CoworkVerifyRole(role)
        )
    except ValueError as exc:
        raise ValueError("Unknown Co-work Verify role") from exc
    return f"{normalized_job_id}{_VERIFY_JOB_MARKER}{normalized_role.value}"


def cowork_verify_job_from_session(
    session_id: str | None,
) -> CoworkVerifyJobIdentity | None:
    """Parse a server-authored Verify worker identity, or return ``None``."""

    normalized = str(session_id or "").strip()
    job_id, marker, raw_role = normalized.rpartition(_VERIFY_JOB_MARKER)
    if not marker or not _GENERATION_RE.fullmatch(job_id):
        return None
    try:
        role = CoworkVerifyRole(raw_role)
    except ValueError:
        return None
    return CoworkVerifyJobIdentity(job_id=job_id, role=role)


def cowork_truth_analysis_session_id(run_id: str) -> str:
    """Return the least-authority hosted-worker identity for one Truth run."""

    normalized = str(run_id or "").strip()
    if not _GENERATION_RE.fullmatch(normalized):
        raise ValueError("Co-work Truth analysis run id is not a safe session identity")
    return f"{normalized}{_TRUTH_ANALYSIS_SUFFIX}"


def cowork_truth_analysis_run_from_session(session_id: str | None) -> str | None:
    """Recover a server-authored Truth analysis run identity, if present."""

    normalized = str(session_id or "").strip()
    if not normalized.endswith(_TRUTH_ANALYSIS_SUFFIX):
        return None
    run_id = normalized[: -len(_TRUTH_ANALYSIS_SUFFIX)]
    return run_id if _GENERATION_RE.fullmatch(run_id) else None


__all__ = [
    "CoworkVerifyJobIdentity",
    "CoworkVerifyRole",
    "cowork_execution_session_id",
    "cowork_generation_from_session",
    "cowork_truth_analysis_run_from_session",
    "cowork_truth_analysis_session_id",
    "cowork_verify_job_from_session",
    "cowork_verify_job_session_id",
]
