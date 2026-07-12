"""Composite per-session view: the observed row, optionally enriched.

One assembler shared by the single-session capability (`conversation_observability_get`)
and the context collector, so both produce the same per-session shape. "Tell me
everything about this session" is one call with opt-in flags rather than four
separate reads (observed row + summary + commits + writes + PRs).

The observed row and the LLM summary are deliberately kept as distinct sub-objects
rather than flattened: their `status`/`error` mean different things (transcript
parse health vs summary-generation health) and fail independently, so a summary
that is errored or not-yet-generated must not make the observed row look broken.
"""

from __future__ import annotations

from typing import Any


def assemble_session_detail(
    observed_row: dict[str, Any],
    *,
    summary: dict[str, Any] | None = None,
    commits: list[dict[str, Any]] | None = None,
    writes: list[dict[str, Any]] | None = None,
    prs: list[dict[str, Any]] | None = None,
    include_summary: bool = False,
    include_commits: bool = False,
    include_writes: bool = False,
    include_prs: bool = False,
) -> dict[str, Any]:
    """Merge an observed row with optional pre-fetched joins into one dict.

    With every ``include_*`` flag False the result is a plain copy of
    ``observed_row``. Each flag adds exactly one key (``summary`` / ``commits``
    / ``writes`` / ``prs``). Callers that batch (the collector) pass their
    already-pre-fetched slices here; the single-session composite passes the
    per-session reads. Kept side-effect-free so both paths share it.
    """
    detail = dict(observed_row)
    if include_summary:
        detail["summary"] = summary
    if include_commits:
        detail["commits"] = commits or []
    if include_writes:
        detail["writes"] = writes or []
    if include_prs:
        detail["prs"] = prs or []
    return detail


def session_detail(
    session_id: str,
    *,
    include_summary: bool = False,
    include_commits: bool = False,
    include_writes: bool = False,
    include_prs: bool = False,
    include_topics: bool = False,
) -> dict[str, Any] | None:
    """Return one session's observed row, optionally enriched by opt-in flags.

    Flags default off, so the bare call is identical in content to the raw
    observed-session row. ``include_topics`` implies ``include_summary`` and
    keeps the summary's per-topic timeline (dropped otherwise to stay compact).
    Returns ``None`` when the session was never observed.
    """
    from work_buddy.conversation_observability.sessions import query_observed_session

    row = query_observed_session(session_id)
    if row is None:
        return None

    if include_topics:
        include_summary = True

    summary: dict[str, Any] | None = None
    if include_summary:
        from work_buddy.conversation_observability.session_summary_row import (
            session_summary_row,
        )

        summary = session_summary_row(session_id)
        if summary is not None and not include_topics:
            summary = {k: v for k, v in summary.items() if k != "topics"}

    commits = writes = prs = None
    if include_commits:
        from work_buddy.conversation_observability.commits import query_session_commits

        commits = query_session_commits(session_id=session_id)
    if include_writes:
        from work_buddy.conversation_observability.writes import query_session_writes

        writes = query_session_writes(session_id=session_id)
    if include_prs:
        from work_buddy.conversation_observability.prs import query_session_prs

        prs = query_session_prs(session_id=session_id)

    return assemble_session_detail(
        row,
        summary=summary,
        commits=commits,
        writes=writes,
        prs=prs,
        include_summary=include_summary,
        include_commits=include_commits,
        include_writes=include_writes,
        include_prs=include_prs,
    )
