"""Compact recent-Claude-Code-activity rendering for context bundles.

Sibling to ``session_activity_collector`` (current-session MCP ledger)
and ``chat_collector`` (raw chat inventory): this collector produces an
**interpreted** summary of recent Claude Code work — sessions, their
commits, and any files they left uncommitted — sourced from the
conversation-observability DB.

Output format (markdown, prompt-ready). Session-level only by default::

    ## Claude Session Summary

    ### work-buddy
    - 10:42–11:31 [9a4c2d11] 12 turns, 2 commits
      Commits: deadbee Add NeverExpires trigger; cafebab Refactor foo
      Uncommitted: tests/unit/test_x.py (M)

    ### secondbrain
    - 14:05–14:22 [88a01ab2] 6 turns, no commits, no dirty files

With ``include_topics=True`` (v2 P6 / PRD F15 / OQ5 resolution), each
session bullet nests a topic-level timeline with absolute span_ranges
and (when available) timestamps::

    ### work-buddy
    - 10:42–11:31 [9a4c2d11] 12 turns, 2 commits
      tldr: Wired the v2 incremental algorithm.
      Topics:
        - [0-8] PRD review and OQ resolution
        - [9-25] Implementation of incremental refresh
        - [26-44] Tests + commit
      Commits: ...

Empty output if the DB has no observed sessions for the window.
Designed to be safe to import when the conversation_observability DB
doesn't exist yet (returns an empty string rather than raising) so the
context bundle stays robust during cold-start.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from work_buddy.timefmt import format_session_span


def collect(cfg: dict[str, Any]) -> str:
    """Return a markdown summary of recent Claude Code session activity.

    Reads from the conversation-observability DB. Honors:
      * ``days`` (int, default 7) — how far back to include.
      * ``project`` (str, optional) — filter to one project.
      * ``refresh`` (bool, default True) — run a stale-only refresh of
        observed_sessions + commits + writes before rendering. Pass
        ``False`` in time-sensitive contexts where you want the DB
        snapshot as-is.
      * ``include_tldr`` (bool, default False) — if set, surface the
        cached LLM tldr (when available) alongside each session's
        attribution line. The LLM is **not** invoked here; this only
        renders existing rows. Generation is gated on the
        ``conversation_observability.summaries.enabled`` config flag
        and runs from the sidecar refresh job.
      * ``include_topics`` (bool, default False) — v2 (PRD F15): if set,
        nest a topic-level timeline under each session bullet using
        v2's per-topic timestamps + span_ranges. No LLM is invoked
        here; rendering reads existing rows from `summarization.db`.
        Implies ``include_tldr=True`` for consistency.
    """
    days = int(cfg.get("days", 7))
    project = cfg.get("project")
    refresh = bool(cfg.get("refresh", True))
    include_tldr = bool(cfg.get("include_tldr", False))
    include_topics = bool(cfg.get("include_topics", False))
    if include_topics:
        include_tldr = True  # topics imply tldr in the rendered output

    try:
        from work_buddy.conversation_observability.commits import (
            query_session_commits,
            refresh_session_commits,
        )
        from work_buddy.conversation_observability.session_summary_row import (
            session_summary_row,
        )
        from work_buddy.conversation_observability.sessions import (
            list_observed_sessions,
            refresh_observed_sessions,
        )
        from work_buddy.conversation_observability.writes import (
            query_session_writes,
            refresh_session_writes,
        )
    except Exception:
        return _empty("conversation_observability package unavailable")

    if refresh:
        try:
            refresh_observed_sessions(days=days, stale_only=True)
            refresh_session_commits(days=days)
            refresh_session_writes(days=days)
        except Exception as exc:  # pragma: no cover — best-effort
            return _empty(f"refresh failed: {type(exc).__name__}")

    try:
        sessions = list_observed_sessions(days=days, project=project)
    except Exception as exc:  # pragma: no cover
        return _empty(f"DB read failed: {type(exc).__name__}")

    if not sessions:
        return _empty("No observed Claude Code sessions in the window.")

    # Pre-fetch commits + writes for every session in one pass.
    all_commits = query_session_commits(days=days)
    commits_by_sid: dict[str, list[dict[str, Any]]] = {}
    for c in all_commits:
        commits_by_sid.setdefault(c["session_id"], []).append(c)

    by_project: dict[str, list[dict[str, Any]]] = {}
    for s in sessions:
        proj = s.get("project_name") or "(unknown)"
        by_project.setdefault(proj, []).append(s)

    lines: list[str] = ["## Claude Session Summary", ""]
    for proj in sorted(by_project):
        lines.append(f"### {proj}")
        proj_sessions = sorted(
            by_project[proj],
            key=lambda r: r.get("end_time") or "",
            reverse=True,
        )
        for s in proj_sessions:
            sid = s["session_id"]
            short_sid = sid[:8]
            time_range = _format_time_range(s.get("start_time"), s.get("end_time"))
            turn_count = s.get("message_count") or 0
            session_commits = commits_by_sid.get(sid, [])
            try:
                session_writes = query_session_writes(session_id=sid)
            except Exception:
                session_writes = []
            dirty_writes = [
                w for w in session_writes
                if w.get("currently_dirty") and not w.get("committed_sha")
            ]

            descriptors: list[str] = [f"{turn_count} turns"]
            if session_commits:
                descriptors.append(
                    f"{len(session_commits)} commit{'s' if len(session_commits) != 1 else ''}"
                )
            else:
                descriptors.append("no commits")
            if dirty_writes:
                descriptors.append(
                    f"{len(dirty_writes)} uncommitted file{'s' if len(dirty_writes) != 1 else ''}"
                )

            lines.append(
                f"- {time_range} [{short_sid}] " + ", ".join(descriptors)
            )

            if include_tldr:
                try:
                    summary_row = session_summary_row(sid)
                except Exception:
                    summary_row = None
                if (
                    summary_row is not None
                    and summary_row.get("status") == "ok"
                    and summary_row.get("tldr")
                ):
                    lines.append(f"  tldr: {summary_row['tldr']}")
                    if include_topics:
                        topics = summary_row.get("topics") or []
                        if topics:
                            lines.append("  Topics:")
                            for t in topics:
                                title = t.get("title", "(untitled)")
                                s_start = t.get("span_start")
                                s_end = t.get("span_end")
                                t_start = t.get("turn_start")
                                t_end = t.get("turn_end")
                                # Prefer turn-index range; fall back to span_range.
                                if isinstance(t_start, int) and isinstance(t_end, int):
                                    range_str = f"turns {t_start}-{t_end}"
                                elif isinstance(s_start, int) and isinstance(s_end, int):
                                    range_str = f"spans {s_start}-{s_end}"
                                else:
                                    range_str = ""
                                line = f"    - {title}"
                                if range_str:
                                    line += f" ({range_str})"
                                summary_text = (t.get("summary") or "").strip()
                                if summary_text:
                                    line += f" — {summary_text}"
                                lines.append(line)

            if session_commits:
                summaries = [
                    f"{c['hash'][:7]} {(c.get('message') or '').splitlines()[0][:60]}"
                    for c in session_commits[:3]
                ]
                lines.append(f"  Commits: {'; '.join(summaries)}")
                if len(session_commits) > 3:
                    lines.append(
                        f"  …and {len(session_commits) - 3} more"
                    )

            if dirty_writes:
                files = [
                    Path(w["file_path"]).name for w in dirty_writes[:5]
                ]
                tail = (
                    f" (+{len(dirty_writes) - 5} more)"
                    if len(dirty_writes) > 5
                    else ""
                )
                lines.append(f"  Uncommitted: {', '.join(files)}{tail}")

            if s.get("status") == "error":
                lines.append(
                    f"  _Observation error: {s.get('error', 'unknown')}_"
                )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _empty(reason: str) -> str:
    return f"## Claude Session Summary\n\n_{reason}_\n"


def _format_time_range(start: str | None, end: str | None) -> str:
    """Render a compact ``HH:MM–HH:MM`` (same-day) or date range, in local time."""
    return format_session_span(start, end, empty="—")
