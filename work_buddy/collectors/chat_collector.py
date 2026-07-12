"""Collect recent agent-harness conversations and legacy CLI chat history."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy import paths
from work_buddy.timefmt import (
    format_session_span,
    parse_iso,
    to_local_naive,
)

# Cache for parsed JSONL summaries — avoids re-parsing unchanged files.
# Bump _CACHE_VERSION when the parsed schema changes (new fields, renamed keys, etc.)
# to auto-invalidate stale entries.
_CACHE_PATH = paths.data_dir("cache") / "transcript_summaries.json"
_CACHE_VERSION = 3  # v3: harness/provider metadata and Codex transcripts

# In-process memo of the parsed cache file, keyed on its (mtime, size). The
# file holds hundreds of parsed-session summaries and json.load of it cost
# ~400ms on every Chats-tab load; this keeps the parse off warm requests.
# _save_cache rewrites the file (changing mtime/size), so a save naturally
# invalidates this memo on the next read.
_parsed_cache: dict[str, Any] | None = None
_parsed_cache_state: tuple[float, int] | None = None


def _load_cache() -> dict[str, Any]:
    """Load the conversation summary cache, discarding if version mismatches.

    Memoized on the cache file's (mtime, size); returns a shallow copy so a
    caller adding newly-parsed entries (then saving) doesn't mutate the memo.
    """
    global _parsed_cache, _parsed_cache_state
    if not _CACHE_PATH.exists():
        return {}
    try:
        st = _CACHE_PATH.stat()
        state: tuple[float, int] | None = (st.st_mtime, st.st_size)
    except OSError:
        state = None
    if (
        state is not None
        and state == _parsed_cache_state
        and _parsed_cache is not None
    ):
        return dict(_parsed_cache)
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if data.get("_version") != _CACHE_VERSION:
        return {}
    _parsed_cache = data
    _parsed_cache_state = state
    return dict(data)


def _save_cache(cache: dict[str, Any]) -> None:
    """Save the conversation summary cache with version stamp."""
    cache["_version"] = _CACHE_VERSION
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except OSError:
        pass


def _cache_key(path: Path) -> str:
    """Generate a cache key from file path, mtime, and size."""
    try:
        stat = path.stat()
        return f"{path}:{stat.st_mtime:.6f}:{stat.st_size}"
    except OSError:
        return ""


def _parse_specstory_filename_ts(ts_str: str) -> datetime | None:
    """Parse a SpecStory filename timestamp (e.g. ``2026-04-01_15-44-10Z``).

    Returns a UTC-aware datetime, or ``None`` if the stamp doesn't parse. The
    trailing ``Z`` is optional; a stamp without an offset is assumed UTC.
    """
    norm = ts_str.replace("Z", "+0000")
    for fmt in ("%Y-%m-%d_%H-%M-%S%z", "%Y-%m-%d_%H-%M-%S"):
        try:
            dt = datetime.strptime(norm, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _find_specstory_files(
    repos_root: Path, days: int, since: str | None = None, until: str | None = None,
) -> list[dict]:
    """Find recent SpecStory history files across all repos."""
    cutoff = datetime.fromisoformat(since).astimezone(timezone.utc) if since else (datetime.now(timezone.utc) - timedelta(days=days))
    upper = datetime.fromisoformat(until).astimezone(timezone.utc) if until else datetime.now(timezone.utc)
    results = []

    if not repos_root.is_dir():
        return results

    for repo_dir in sorted(repos_root.iterdir()):
        if not repo_dir.is_dir():
            continue
        history_dir = repo_dir / ".specstory" / "history"
        if not history_dir.is_dir():
            continue

        for md_file in sorted(history_dir.glob("*.md"), reverse=True):
            try:
                mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue

            if mtime < cutoff or mtime > upper:
                continue

            # Parse timestamp and title from filename
            # Format: 2026-04-01_15-44-10Z-agent-instructions-modification.md
            name = md_file.stem
            ts_match = re.match(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z?)-(.*)", name)
            real_dt = None
            if ts_match:
                real_dt = _parse_specstory_filename_ts(ts_match.group(1))
                title_slug = ts_match.group(2).replace("-", " ").title()
            else:
                title_slug = name

            # Window on the session's real time (the filename stamp) when we
            # have it; the mtime check above is only a cheap pre-filter. Fall
            # back to the mtime decision (already passed) when the filename has
            # no parseable timestamp, so undated sessions aren't dropped.
            if real_dt is not None and (real_dt < cutoff or real_dt > upper):
                continue

            # Display the session's real local time, falling back to mtime.
            ts_str = to_local_naive(real_dt or mtime).strftime("%Y-%m-%d %H:%M")

            # Read first user message as preview
            preview = _extract_preview(md_file)

            results.append({
                "repo": repo_dir.name,
                "timestamp": ts_str,
                "title": title_slug,
                "preview": preview,
                "file": md_file.relative_to(repos_root).as_posix(),
            })

    return results


def _extract_preview(file_path: Path, max_chars: int = 500) -> str:
    """Extract the first user message from a SpecStory markdown file."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    # SpecStory format has _User_ or **User** markers for user messages
    # Look for the first user turn
    patterns = [
        re.compile(r"^_User_\s*$", re.MULTILINE),
        re.compile(r"^\*\*User\*\*", re.MULTILINE),
        re.compile(r"^## User", re.MULTILINE),
        re.compile(r"^### Human", re.MULTILINE),
    ]

    for pattern in patterns:
        match = pattern.search(content)
        if match:
            start = match.end()
            # Find the next assistant/agent marker
            end_patterns = [
                re.compile(r"^_(?:Assistant|Agent)_", re.MULTILINE),
                re.compile(r"^\*\*(?:Assistant|Agent)\*\*", re.MULTILINE),
                re.compile(r"^## (?:Assistant|Agent)", re.MULTILINE),
            ]
            end = len(content)
            for ep in end_patterns:
                em = ep.search(content, start)
                if em:
                    end = min(end, em.start())

            preview = content[start:end].strip()
            if len(preview) > max_chars:
                preview = preview[:max_chars] + "..."
            return preview

    # Fallback: just take the first non-frontmatter content
    lines = content.split("\n")
    body_lines = []
    in_frontmatter = False
    for line in lines:
        if line.strip() == "---":
            in_frontmatter = not in_frontmatter
            continue
        if not in_frontmatter and line.strip():
            body_lines.append(line)
            if len("\n".join(body_lines)) > max_chars:
                break

    return "\n".join(body_lines)[:max_chars]


def _parse_claude_history(
    days: int, since: str | None = None, until: str | None = None,
) -> list[dict]:
    """Parse ~/.claude/history.jsonl for recent CLI sessions."""
    history_path = Path.home() / ".claude" / "history.jsonl"
    if not history_path.exists():
        return []

    cutoff_dt = datetime.fromisoformat(since).astimezone(timezone.utc) if since else (datetime.now(timezone.utc) - timedelta(days=days))
    upper_dt = datetime.fromisoformat(until).astimezone(timezone.utc) if until else datetime.now(timezone.utc)
    cutoff_ms = int(cutoff_dt.timestamp() * 1000)
    upper_ms = int(upper_dt.timestamp() * 1000)

    sessions: dict[str, dict] = {}
    try:
        with open(history_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = entry.get("timestamp", 0)
                if ts < cutoff_ms or ts > upper_ms:
                    continue

                session_id = entry.get("sessionId", "unknown")
                project = entry.get("project", "")
                display = entry.get("display", "").strip()

                if session_id not in sessions:
                    sessions[session_id] = {
                        "session_id": session_id[:8],
                        "project": project,
                        "start": ts,
                        "commands": [],
                    }

                if display:
                    sessions[session_id]["commands"].append(display)
                # Track the latest timestamp
                sessions[session_id]["end"] = ts

    except OSError:
        return []

    # Convert to list and sort by start time
    result = sorted(sessions.values(), key=lambda x: x["start"], reverse=True)
    for s in result:
        s["start_str"] = to_local_naive(
            datetime.fromtimestamp(s["start"] / 1000, tz=timezone.utc)
        ).strftime("%Y-%m-%d %H:%M")
        # Extract project name from path
        if s["project"]:
            s["project_name"] = Path(s["project"]).name
        else:
            s["project_name"] = "unknown"

    return result


def iter_session_turns(path: Path):
    """Yield canonical turns from any registered transcript path."""
    from work_buddy.transcripts import provider_for_session, session_from_path

    try:
        session = session_from_path(path)
    except FileNotFoundError:
        return
    provider = provider_for_session(session)
    for turn in provider.iter_turns(session):
        yield turn.to_dict()


def _parse_session_jsonl(path: Path) -> dict | None:
    """Extract summary info from a single Claude Code JSONL session file.

    Uses iter_session_turns() for parsing, then aggregates into the summary
    format expected by the chat collector.
    """
    from work_buddy.transcripts import session_from_path

    try:
        session = session_from_path(path)
    except FileNotFoundError:
        return None
    session_id = session.session_id
    first_user_msg = ""
    user_messages: list[str] = []
    user_count = 0
    assistant_text_count = 0
    tool_names: dict[str, int] = {}
    assistant_snippets: list[str] = []
    all_assistant_text: list[str] = []
    start_time = None
    end_time = None

    for turn in iter_session_turns(path):
        ts = turn["timestamp"]
        if ts:
            if start_time is None:
                start_time = ts
            end_time = ts

        if turn["role"] == "user":
            user_count += 1
            text = turn["text"]
            user_messages.append(text)
            if not first_user_msg:
                first_user_msg = text

        elif turn["role"] == "assistant":
            for name in turn["tools"]:
                tool_names[name] = tool_names.get(name, 0) + 1

            text = turn["text"]
            if text:
                assistant_text_count += 1
                all_assistant_text.append(text[:500])
                if len(text) > 20 and len(assistant_snippets) < 5:
                    assistant_snippets.append(text[:300])

    if user_count == 0:
        return None

    total_tool_calls = sum(tool_names.values())

    return {
        "session_id": session_id[:8],
        "full_session_id": session_id,
        "native_session_id": session.native_session_id,
        "harness_id": session.harness_id,
        "provider_id": session.provider_id,
        "harness_label": session.originator or session.harness_id,
        "project_slug": session.project_slug,
        "project_name": session.project_name,
        "cwd": session.cwd,
        "first_user_message": first_user_msg[:500],
        "user_messages": user_messages,
        "user_msg_count": user_count,
        "assistant_text_count": assistant_text_count,
        "tool_use_count": total_tool_calls,
        "tool_names": tool_names,
        "assistant_snippets": assistant_snippets,
        "all_assistant_text": all_assistant_text,
        "start_time": start_time,
        "end_time": end_time,
    }


def _slug_to_readable(slug: str) -> str:
    """Pure-string heuristic: extract a readable name from a project slug.

    This is the low-level building block.  External callers should use
    :func:`project_name_from_slug` which also resolves subdirectory
    sessions to their parent project.
    """
    if "--" in slug:
        slug = slug.split("--", 1)[1]
    parts = slug.split("-")
    if len(parts) >= 2:
        if parts[-2].lower() in ("repos", "projects", "src", "code", "dev", "home"):
            return parts[-1]
        return f"{parts[-2]}-{parts[-1]}"
    return parts[-1] if parts else slug


# Cache: dirname -> resolved name (stable for a process's lifetime)
_project_name_cache: dict[str, str] = {}


def project_name_from_slug(slug: str) -> str:
    """Canonical project name resolver — the ONE function to use everywhere.

    Handles subdirectory sessions (e.g. ``my-project-feature-x``) by checking
    whether this slug's Claude projects directory is a child of another
    project directory.  If so, maps to the parent's name.

    Falls back to a pure-string heuristic when no parent directory is found.
    Results are cached per-process.
    """
    if slug in _project_name_cache:
        return _project_name_cache[slug]

    from work_buddy.transcripts.providers.claude import (
        project_name_from_slug as _resolve,
    )

    resolved = _resolve(slug)

    _project_name_cache[slug] = resolved
    return resolved


def _format_duration(start: str | None, end: str | None) -> str:
    """Format a duration string from ISO timestamps."""
    if not start or not end:
        return ""
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
        delta = t1 - t0
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return f"{total_seconds}s"
        minutes = total_seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        remaining_min = minutes % 60
        if remaining_min:
            return f"{hours}h {remaining_min}m"
        return f"{hours}h"
    except (ValueError, TypeError):
        return ""


def _format_session_when(
    start: str | None, end: str | None, fallback: str = ""
) -> str:
    """Render when a conversation actually happened, in the user's local time.

    Prefers the conversation's own ``start_time``/``end_time`` (the
    authoritative "when did this happen" signal, parsed from the message
    timestamps) over the JSONL file mtime. The file mtime drifts to the
    present whenever a session is resumed or rewritten, so it does NOT
    reflect when the conversation took place — rendering it as the session
    time silently misdates resumed sessions by days. Falls back to
    *fallback* only when neither timestamp is available.
    """
    return format_session_span(start, end, fallback=fallback)


def _get_agent_conversations(
    days: int,
    project_filter: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    provider_ids: list[str] | None = None,
) -> list[dict]:
    """Collect recent conversations from enabled transcript providers.

    Parses each session directly (no external dependency) and returns summaries.
    Uses a file-based cache to skip re-parsing unchanged JSONL files.

    Args:
        days: Only include sessions modified within the last N days.
        project_filter: Optional list of project slug substrings to include.
            If None, includes all projects. E.g., ["work-buddy", "my-project"]
            matches any project slug containing those strings.
        since: ISO datetime for range start (overrides days).
        until: ISO datetime for range end.
    """
    if since:
        cutoff = datetime.fromisoformat(since).astimezone(timezone.utc)
    elif days <= 0:
        # ``days=0`` (or negative) is the dashboard's "All time" sentinel.
        # The frontend exposes this as the last entry in the days dropdown
        # for users who want unbounded history. We use epoch-zero so the
        # mtime check below trivially passes for every JSONL file.
        cutoff = datetime.fromtimestamp(0, tz=timezone.utc)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    upper = datetime.fromisoformat(until).astimezone(timezone.utc) if until else datetime.now(timezone.utc)
    cache = _load_cache()
    cache_dirty = False
    results = []

    from work_buddy.transcripts import discover_sessions

    # Push the window start down into provider discovery as a coarse mtime
    # floor (skips old files without opening them). The upper bound stays here:
    # a resumed file can carry a recent mtime yet old turns, so mtime is a valid
    # lower bound but not an upper one — the precise conversation-time overlap
    # below owns the upper bound whenever real timestamps are available.
    sessions = discover_sessions(
        since=cutoff,
        until=upper,
        project_filter=project_filter,
        provider_ids=provider_ids,
    )
    for session in sessions:
        mtime = datetime.fromtimestamp(session.mtime, tz=timezone.utc)
        if mtime < cutoff:
            continue
        key = _cache_key(session.path)
        if key and key in cache:
            summary = cache[key]
        else:
            summary = _parse_session_jsonl(session.path)
            if summary is not None and key:
                cache[key] = summary
                cache_dirty = True
        if not summary:
            continue

        s_real = parse_iso(summary.get("start_time"))
        e_real = parse_iso(summary.get("end_time"))
        if s_real or e_real:
            # Precise conversation-time overlap with [cutoff, upper].
            s_eff = s_real or e_real
            e_eff = e_real or s_real
            if e_eff < cutoff or s_eff > upper:
                continue
        elif mtime > upper:
            # No parsed timestamps: mtime is the only upper-bound signal.
            continue

        summary_copy = dict(summary)
        summary_copy["modified"] = to_local_naive(mtime).strftime("%Y-%m-%d %H:%M")
        results.append(summary_copy)

    if cache_dirty:
        _save_cache(cache)

    return sorted(results, key=lambda x: x["modified"], reverse=True)


def _get_claude_code_conversations(
    days: int,
    project_filter: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Compatibility wrapper for callers that explicitly want Claude only."""
    return _get_agent_conversations(
        days,
        project_filter,
        since,
        until,
        provider_ids=["claudecode"],
    )


def _window_label(since: str | None, until: str | None, days: int) -> str:
    """Honest window label for a section header.

    Reports the effective window: a precise ``since → until`` span (local time)
    when an explicit window is set, otherwise the day-granular ``last N days``.
    """
    if since or until:
        s = to_local_naive(parse_iso(since)) if since else None
        u = to_local_naive(parse_iso(until)) if until else None
        if s and u:
            return f"{s.strftime('%Y-%m-%d %H:%M')} → {u.strftime('%Y-%m-%d %H:%M')}"
        if s:
            return f"since {s.strftime('%Y-%m-%d %H:%M')}"
        if u:
            return f"until {u.strftime('%Y-%m-%d %H:%M')}"
    return f"last {days} days"


def collect(cfg: dict[str, Any]) -> str:
    """Collect chat context and return markdown string.

    Args:
        cfg: Configuration dict. ``chats.last=N`` caps each source to the N most
            recent sessions; ``chats.include_agent_conversations=False`` drops
            the agent-conversation section (used by context bundles, where the
            interpreted agent_session_summary surface owns those sessions).
    """
    repos_root = Path(cfg["repos_root"])
    chat_cfg = cfg.get("chats", {})
    specstory_days = chat_cfg.get("specstory_days", 7)
    claude_days = chat_cfg.get("claude_history_days", 7)
    last_n = chat_cfg.get("last", None)

    # Explicit time-range overrides (from update-journal / a scoped bundle),
    # read at the top level where the wrapper delivers them.
    range_since = cfg.get("since")
    range_until = cfg.get("until")

    # Whether this file lists agent-harness conversations. In a context bundle
    # the interpreted agent_session_summary surface owns them, so the bundle
    # passes this False and the raw file carries only SpecStory + CLI history.
    include_agent = cfg.get(
        "include_agent_conversations",
        chat_cfg.get("include_agent_conversations", True),
    )

    now = datetime.now(timezone.utc)

    specstory_files = _find_specstory_files(repos_root, specstory_days, since=range_since, until=range_until)
    claude_sessions = _parse_claude_history(claude_days, since=range_since, until=range_until)
    project_filter = chat_cfg.get("project_filter", None)
    agent_conversations: list[dict] = []
    if include_agent:
        agent_conversations = _get_agent_conversations(
            claude_days, project_filter, since=range_since, until=range_until
        )

    # Apply chats.last=N cap to each source
    if last_n is not None:
        specstory_files = specstory_files[:last_n]
        claude_sessions = claude_sessions[:last_n]
        agent_conversations = agent_conversations[:last_n]

    # Nothing in the window: render empty so the bundle writer emits no file
    # rather than a header-only template that wastes the reader's context.
    if not specstory_files and not claude_sessions and not agent_conversations:
        return ""

    specstory_label = _window_label(range_since, range_until, specstory_days)
    history_label = _window_label(range_since, range_until, claude_days)

    lines = [
        "# Chat Summary",
        "",
        f"*Collected: {to_local_naive(now).strftime('%Y-%m-%d %H:%M')}*",
        "",
    ]

    if specstory_files:
        lines.append(f"## SpecStory Sessions ({specstory_label})")
        lines.append("")
        for entry in specstory_files:
            lines.append(f"### [{entry['repo']}] {entry['title']}")
            lines.append(f"*{entry['timestamp']}*")
            lines.append("")
            if entry["preview"]:
                lines.append(f"> {entry['preview'][:300]}")
                lines.append("")
    else:
        lines.append("## SpecStory Sessions")
        lines.append("")
        lines.append("*No recent SpecStory sessions found.*")
        lines.append("")

    if agent_conversations:
        lines.append(f"## Agent Conversations ({history_label})")
        lines.append("")
        for conv in agent_conversations[:20]:
            # Session header with duration
            duration_str = _format_duration(conv.get("start_time"), conv.get("end_time"))
            lines.append(
                f"### [{conv['project_name']}] [{conv.get('harness_label', 'agent')}] "
                f"{conv.get('full_session_id', conv['session_id'])}"
            )
            session_when = _format_session_when(
                conv.get("start_time"),
                conv.get("end_time"),
                fallback=conv.get("modified", ""),
            )
            stats_parts = [
                session_when,
                f"{conv['user_msg_count']} user msgs",
                f"{conv.get('assistant_text_count', conv.get('assistant_msg_count', 0))} responses",
                f"{conv['tool_use_count']} tool calls",
            ]
            if duration_str:
                stats_parts.append(duration_str)
            lines.append(f"*{' — '.join(stats_parts)}*")
            lines.append("")

            # First user message as topic
            if conv.get("first_user_message"):
                preview = conv["first_user_message"][:300]
                # Collapse to first line if it's very long
                first_line = preview.split("\n")[0].strip()
                if len(first_line) > 200:
                    first_line = first_line[:200] + "..."
                lines.append(f"> {first_line}")
                lines.append("")

            # Tool usage summary (top tools)
            tool_names = conv.get("tool_names", {})
            if tool_names:
                # Show top 5 tools by frequency
                sorted_tools = sorted(tool_names.items(), key=lambda x: x[1], reverse=True)[:5]
                tool_strs = [f"{name} ({count})" for name, count in sorted_tools]
                lines.append(f"Tools: {', '.join(tool_strs)}")
                lines.append("")

            # Assistant response snippet (first meaningful one)
            snippets = conv.get("assistant_snippets", [])
            if snippets:
                # Use the first snippet as outcome/summary
                snippet = snippets[0].split("\n")[0].strip()
                if len(snippet) > 200:
                    snippet = snippet[:200] + "..."
                lines.append(f"Outcome: {snippet}")
                lines.append("")
    elif include_agent:
        lines.append("## Agent Conversations")
        lines.append("")
        lines.append("*No recent agent conversations found.*")
        lines.append("")

    if claude_sessions:
        lines.append(f"## Claude Code CLI History ({history_label})")
        lines.append("")
        for session in claude_sessions[:20]:
            cmd_count = len(session["commands"])
            lines.append(
                f"- **{session['project_name']}** ({session['start_str']}) "
                f"— {cmd_count} command(s): "
                f"`{'`, `'.join(session['commands'][:5])}`"
            )
            if cmd_count > 5:
                lines.append(f"  *(+{cmd_count - 5} more)*")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Search: full-text keyword search across conversations
# ---------------------------------------------------------------------------

def _extract_match_snippets(
    query_lower: str,
    texts: list[str],
    *,
    max_snippets: int = 3,
    context_chars: int = 80,
) -> list[str]:
    """Extract short snippets surrounding each match of *query_lower* in *texts*.

    Returns up to *max_snippets* unique snippets, each showing
    *context_chars* of surrounding text around the match.
    """
    snippets: list[str] = []
    seen: set[str] = set()

    for text in texts:
        text_lower = text.lower()
        start = 0
        while start < len(text_lower) and len(snippets) < max_snippets:
            idx = text_lower.find(query_lower, start)
            if idx == -1:
                break

            # Extract surrounding context
            snip_start = max(0, idx - context_chars)
            snip_end = min(len(text), idx + len(query_lower) + context_chars)
            snippet = text[snip_start:snip_end].strip()

            # Clean up: collapse whitespace, add ellipsis markers
            snippet = " ".join(snippet.split())
            if snip_start > 0:
                snippet = "..." + snippet
            if snip_end < len(text):
                snippet = snippet + "..."

            # Deduplicate near-identical snippets
            dedup_key = snippet[:60].lower()
            if dedup_key not in seen:
                seen.add(dedup_key)
                snippets.append(snippet)

            start = idx + len(query_lower)

    return snippets


def search_conversations(
    query: str,
    *,
    days: int = 7,
    last: int = 20,
    show_context: bool = True,
) -> str:
    """Search enabled agent-harness conversations by keyword.

    Matches against all user messages, all assistant text blocks, tool
    names, and project name.  Returns formatted markdown with matching
    sessions.

    Args:
        query: Search term (case-insensitive substring match).
        days: Lookback window for session discovery.
        last: Maximum sessions to search across.
        show_context: If True, include snippets showing where the query
            matched within the conversation.
    """
    from work_buddy.config import load_config

    cfg = load_config()
    project_filter = cfg.get("chats", {}).get("project_filter", None)

    conversations = _get_agent_conversations(days, project_filter)
    if last:
        conversations = conversations[:last]

    query_lower = query.lower()
    matches: list[tuple[dict, list[str]]] = []  # (conv, snippets)

    for conv in conversations:
        user_msgs = conv.get("user_messages", [conv.get("first_user_message", "")])
        asst_texts = conv.get("all_assistant_text", conv.get("assistant_snippets", []))
        meta_texts = [
            conv.get("project_name", ""),
            conv.get("project_slug", ""),
            " ".join(conv.get("tool_names", {}).keys()),
        ]

        searchable = " ".join(user_msgs + asst_texts + meta_texts).lower()

        if query_lower in searchable:
            snippets = []
            if show_context:
                snippets = _extract_match_snippets(
                    query_lower, user_msgs + asst_texts
                )
            matches.append((conv, snippets))

    if not matches:
        return f"No conversations matching '{query}' in the last {days} days."

    lines = [
        f"*{len(matches)} match(es) from last {days} days*",
        "",
    ]

    for conv, snippets in matches:
        duration_str = _format_duration(conv.get("start_time"), conv.get("end_time"))
        lines.append(f"### [{conv.get('project_name', '?')}] {conv.get('full_session_id', conv['session_id'])}")

        stats = [
            _format_session_when(
                conv.get("start_time"),
                conv.get("end_time"),
                fallback=conv.get("modified", ""),
            ),
            f"{conv['user_msg_count']} user msgs",
            f"{conv['tool_use_count']} tool calls",
        ]
        if duration_str:
            stats.append(duration_str)
        lines.append(f"*{' — '.join(stats)}*")
        lines.append("")

        if conv.get("first_user_message"):
            first_line = conv["first_user_message"][:300].split("\n")[0].strip()
            if len(first_line) > 200:
                first_line = first_line[:200] + "..."
            lines.append(f"> {first_line}")
            lines.append("")

        # Show match context snippets
        if snippets:
            lines.append("**Matches:**")
            for snip in snippets:
                lines.append(f"- `{snip}`")
            lines.append("")

        tool_names = conv.get("tool_names", {})
        if tool_names:
            sorted_tools = sorted(tool_names.items(), key=lambda x: x[1], reverse=True)[:5]
            tool_strs = [f"{name} ({count})" for name, count in sorted_tools]
            lines.append(f"Tools: {', '.join(tool_strs)}")
            lines.append("")

        snippets = conv.get("assistant_snippets", [])
        if snippets:
            snippet = snippets[0].split("\n")[0].strip()
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            lines.append(f"Outcome: {snippet}")
            lines.append("")

    return "\n".join(lines)
