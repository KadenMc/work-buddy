"""Collect recent Claude/Cursor chat history from SpecStory, Claude CLI, and Claude Code conversations."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Cache for parsed JSONL summaries — avoids re-parsing unchanged files.
# Bump _CACHE_VERSION when the parsed schema changes (new fields, renamed keys, etc.)
# to auto-invalidate stale entries.
_CACHE_PATH = Path.home() / ".claude" / "projects" / "work_buddy_chat_cache.json"
_CACHE_VERSION = 2  # v2: added user_messages, all_assistant_text


def _load_cache() -> dict[str, Any]:
    """Load the conversation summary cache, discarding if version mismatches."""
    if not _CACHE_PATH.exists():
        return {}
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if data.get("_version") != _CACHE_VERSION:
        return {}
    return data


def _save_cache(cache: dict[str, Any]) -> None:
    """Save the conversation summary cache with version stamp."""
    cache["_version"] = _CACHE_VERSION
    try:
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
            if ts_match:
                ts_str = ts_match.group(1)
                title_slug = ts_match.group(2).replace("-", " ").title()
            else:
                ts_str = mtime.strftime("%Y-%m-%d %H:%M")
                title_slug = name

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
        s["start_str"] = datetime.fromtimestamp(
            s["start"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        # Extract project name from path
        if s["project"]:
            s["project_name"] = Path(s["project"]).name
        else:
            s["project_name"] = "unknown"

    return result


def iter_session_turns(path: Path):
    """Yield parsed turns from a Claude Code JSONL session file.

    Handles all edge cases: tool_result user messages (skipped), isMeta entries
    (skipped), list-format user content, assistant text/tool_use blocks.

    Yields:
        dict with keys: role ("user"|"assistant"), text (str), tools (list[str]),
        timestamp (str|None).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type")
                timestamp = entry.get("timestamp")

                if entry_type == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")

                    # Skip tool results
                    if isinstance(content, list):
                        has_tool_result = any(
                            isinstance(c, dict) and c.get("type") == "tool_result"
                            for c in content
                        )
                        if has_tool_result:
                            continue
                        text_parts = [
                            c.get("text", "")
                            for c in content
                            if isinstance(c, dict) and c.get("type") == "text"
                        ]
                        content = " ".join(text_parts)

                    if entry.get("isMeta"):
                        continue

                    if isinstance(content, str) and content.strip():
                        yield {
                            "role": "user",
                            "text": content.strip(),
                            "tools": [],
                            "timestamp": timestamp,
                        }

                elif entry_type == "assistant":
                    msg = entry.get("message", {})
                    content = msg.get("content", [])
                    tools = []
                    texts = []
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype == "tool_use":
                                tools.append(block.get("name", "unknown"))
                            elif btype == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    texts.append(text)

                    if texts or tools:
                        yield {
                            "role": "assistant",
                            "text": " ".join(texts),
                            "tools": tools,
                            "timestamp": timestamp,
                        }
    except OSError:
        return


def _parse_session_jsonl(path: Path) -> dict | None:
    """Extract summary info from a single Claude Code JSONL session file.

    Uses iter_session_turns() for parsing, then aggregates into the summary
    format expected by the chat collector.
    """
    session_id = path.stem
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

    from pathlib import Path
    claude_projects = Path.home() / ".claude" / "projects"

    resolved = slug  # default: just the heuristic
    if claude_projects.is_dir():
        for sibling in claude_projects.iterdir():
            if not sibling.is_dir() or sibling.name == slug:
                continue
            if slug.startswith(sibling.name + "-"):
                resolved = _slug_to_readable(sibling.name)
                break

    if resolved == slug:
        resolved = _slug_to_readable(slug)

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


def _get_claude_code_conversations(
    days: int,
    project_filter: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Scan ~/.claude/projects/ for recent Claude Code conversation JSONL files.

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
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.is_dir():
        return []

    cutoff = datetime.fromisoformat(since).astimezone(timezone.utc) if since else (datetime.now(timezone.utc) - timedelta(days=days))
    upper = datetime.fromisoformat(until).astimezone(timezone.utc) if until else datetime.now(timezone.utc)
    cache = _load_cache()
    cache_dirty = False
    results = []

    for project_dir in sorted(claude_dir.iterdir()):
        if not project_dir.is_dir():
            continue

        project_slug = project_dir.name

        # Project filter: skip projects that don't match any filter substring
        if project_filter:
            if not any(f.lower() in project_slug.lower() for f in project_filter):
                continue

        for jsonl_file in sorted(project_dir.glob("*.jsonl"), reverse=True):
            if "subagents" in str(jsonl_file):
                continue

            try:
                mtime = datetime.fromtimestamp(
                    jsonl_file.stat().st_mtime, tz=timezone.utc
                )
                if mtime < cutoff or mtime > upper:
                    continue
            except OSError:
                continue

            # Check cache
            key = _cache_key(jsonl_file)
            if key and key in cache:
                summary = cache[key]
            else:
                summary = _parse_session_jsonl(jsonl_file)
                if summary is not None and key:
                    cache[key] = summary
                    cache_dirty = True

            if not summary:
                continue

            # Derive readable project name from slug
            # Slugs look like "C--path-to-repos-work-buddy"
            # The slug is the path with separators replaced by dashes
            # Extract the last meaningful path segment
            project_name = project_name_from_slug(project_slug)

            summary_copy = dict(summary)
            summary_copy["project_slug"] = project_slug
            summary_copy["project_name"] = project_name
            summary_copy["modified"] = mtime.strftime("%Y-%m-%d %H:%M")
            results.append(summary_copy)

    if cache_dirty:
        _save_cache(cache)

    return sorted(results, key=lambda x: x["modified"], reverse=True)


def collect(cfg: dict[str, Any]) -> str:
    """Collect chat context and return markdown string.

    Args:
        cfg: Configuration dict. Set ``chats.last=N`` to only include the N
            most recent sessions from each source.
    """
    repos_root = Path(cfg["repos_root"])
    chat_cfg = cfg.get("chats", {})
    specstory_days = chat_cfg.get("specstory_days", 7)
    claude_days = chat_cfg.get("claude_history_days", 7)
    last_n = chat_cfg.get("last", None)

    # Explicit time range overrides (from update-journal workflow)
    range_since = cfg.get("since")
    range_until = cfg.get("until")

    now = datetime.now(timezone.utc)

    specstory_files = _find_specstory_files(repos_root, specstory_days, since=range_since, until=range_until)
    claude_sessions = _parse_claude_history(claude_days, since=range_since, until=range_until)
    project_filter = chat_cfg.get("project_filter", None)
    claude_conversations = _get_claude_code_conversations(claude_days, project_filter, since=range_since, until=range_until)

    # Apply chats.last=N cap to each source
    if last_n is not None:
        specstory_files = specstory_files[:last_n]
        claude_sessions = claude_sessions[:last_n]
        claude_conversations = claude_conversations[:last_n]

    lines = [
        "# Chat Summary",
        "",
        f"*Collected: {now.strftime('%Y-%m-%d %H:%M UTC')}*",
        "",
    ]

    if specstory_files:
        lines.append(f"## SpecStory Sessions (last {specstory_days} days)")
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

    if claude_conversations:
        lines.append(f"## Claude Code Conversations (last {claude_days} days)")
        lines.append("")
        for conv in claude_conversations[:20]:
            # Session header with duration
            duration_str = _format_duration(conv.get("start_time"), conv.get("end_time"))
            lines.append(
                f"### [{conv['project_name']}] {conv.get('full_session_id', conv['session_id'])}"
            )
            stats_parts = [
                conv['modified'],
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
    else:
        lines.append("## Claude Code Conversations")
        lines.append("")
        lines.append("*No recent Claude Code conversations found.*")
        lines.append("")

    if claude_sessions:
        lines.append(f"## Claude Code CLI History (last {claude_days} days)")
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
    """Search Claude Code conversations by keyword.

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

    conversations = _get_claude_code_conversations(days, project_filter)
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
            conv.get("modified", ""),
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
