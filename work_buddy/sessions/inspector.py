"""Session-level conversation inspection for the MCP gateway.

Provides random-access into individual Claude Code conversation sessions:
paginated message browsing, context expansion around a specific message,
mapping from IR search hits (span_index) back to conversation turns,
and structured extraction of git commits made during sessions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from work_buddy.collectors.chat_collector import iter_session_turns, _format_duration
from work_buddy.config import load_config


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------

_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def resolve_session_path(session_id: str) -> tuple[Path, str]:
    """Find a JSONL session file from a full or partial (prefix) session ID.

    Returns ``(path, full_session_id)``.
    Raises :class:`FileNotFoundError` if no match or multiple ambiguous matches.

    This is the canonical session-ID resolver.  All code that needs to go from
    a user-supplied (possibly partial) session ID to the authoritative full UUID
    should call this function or :func:`resolve_session_id`.
    """
    if not _CLAUDE_PROJECTS.is_dir():
        raise FileNotFoundError("Claude projects directory not found")

    matches: list[tuple[Path, str]] = []
    for project_dir in _CLAUDE_PROJECTS.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            if "subagents" in str(jsonl):
                continue
            stem = jsonl.stem
            if stem == session_id or stem.startswith(session_id):
                matches.append((jsonl, stem))

    if not matches:
        raise FileNotFoundError(f"No session found matching '{session_id}'")
    if len(matches) > 1:
        # Check for exact match first
        exact = [m for m in matches if m[1] == session_id]
        if len(exact) == 1:
            return exact[0]
        ids = [m[1][:12] for m in matches[:5]]
        raise FileNotFoundError(
            f"Ambiguous session ID '{session_id}' matches {len(matches)} sessions: {ids}..."
        )
    return matches[0]


def resolve_session_id(session_id: str) -> str:
    """Resolve a partial session ID prefix to the full UUID.

    Convenience wrapper around :func:`resolve_session_path` for callers that
    only need the ID, not the filesystem path.

    Raises :class:`FileNotFoundError` on zero or ambiguous matches.
    """
    _, full_id = resolve_session_path(session_id)
    return full_id


# ---------------------------------------------------------------------------
# ConversationSession
# ---------------------------------------------------------------------------


class ConversationSession:
    """Random-access wrapper around a Claude Code JSONL session file.

    Lazy-loads turns on first access and builds a span-to-turn mapping
    that mirrors the IR chunking algorithm for reliable ``session_locate``.
    """

    def __init__(self, session_id: str) -> None:
        self._path, self._session_id = resolve_session_path(session_id)
        self._turns: list[dict] | None = None
        self._span_map: dict[int, tuple[int, int]] | None = None

    # -- lazy loading -------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._turns is not None:
            return
        self._turns = list(iter_session_turns(self._path))
        self._build_span_map()

    def _build_span_map(self) -> None:
        """Replay the IR chunking algorithm to map span_index -> (turn_start, turn_end).

        Must exactly match ``ir/sources/conversations.py:96-151`` to avoid drift.
        """
        cfg = load_config()
        max_turns = (
            cfg.get("ir", {})
            .get("sources", {})
            .get("conversation", {})
            .get("span_max_turns", 4)
        )

        self._span_map = {}
        turns = self._turns
        i = 0
        span_idx = 0
        while i < len(turns):
            end = min(i + max_turns, len(turns))
            span = turns[i:end]
            # Skip empty spans — must exactly match conversations.py:104-111
            # Note: the IR source collects texts by role WITHOUT filtering
            # empty strings, then checks if the *list* is non-empty.  We
            # must replicate that: a span with empty-string user texts still
            # counts as non-empty (the list has items).
            user_texts = [t["text"][:2000] for t in span if t["role"] == "user"]
            asst_texts = [t["text"][:2000] for t in span if t["role"] == "assistant"]
            if user_texts or asst_texts:
                self._span_map[span_idx] = (i, end)
                span_idx += 1
            i = end

    # -- properties ---------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def turns(self) -> list[dict]:
        self._ensure_loaded()
        return self._turns

    @property
    def message_count(self) -> int:
        return len(self.turns)

    @property
    def start_time(self) -> str | None:
        turns = self.turns
        for t in turns:
            if t.get("timestamp"):
                return t["timestamp"]
        return None

    @property
    def end_time(self) -> str | None:
        turns = self.turns
        for t in reversed(turns):
            if t.get("timestamp"):
                return t["timestamp"]
        return None

    @property
    def duration(self) -> str:
        return _format_duration(self.start_time, self.end_time)

    @property
    def tools_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.turns:
            for name in t.get("tools", []):
                counts[name] = counts.get(name, 0) + 1
        return counts

    # -- metadata -----------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        return {
            "session_id": self._session_id,
            "message_count": self.message_count,
            "duration": self.duration,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "tools_summary": self.tools_summary,
        }

    # -- message access -----------------------------------------------------

    def _filter_turns(
        self,
        roles: list[str] | None = None,
        message_types: list[str] | None = None,
        query: str | None = None,
    ) -> list[tuple[int, dict]]:
        """Return (original_index, turn) pairs matching the filters.

        All filters are ANDed: a turn must match all specified criteria.
        ``query`` does case-insensitive substring matching on the turn text.
        """
        query_lower = query.lower() if query else None
        result = []
        for idx, turn in enumerate(self.turns):
            if roles and turn["role"] not in roles:
                continue
            if message_types:
                has_text = bool(turn.get("text"))
                has_tools = bool(turn.get("tools"))
                match = False
                if "text" in message_types and has_text:
                    match = True
                if "tool_use" in message_types and has_tools:
                    match = True
                if not match:
                    continue
            if query_lower and query_lower not in (turn.get("text") or "").lower():
                continue
            result.append((idx, turn))
        return result

    def get_messages(
        self,
        offset: int = 0,
        limit: int = 10,
        roles: list[str] | None = None,
        message_types: list[str] | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        filtered = self._filter_turns(roles, message_types, query)
        page = filtered[offset : offset + limit]
        messages = []
        for orig_idx, turn in page:
            messages.append({
                "index": orig_idx,
                "role": turn["role"],
                "text_preview": turn["text"][:300] if turn.get("text") else "",
                "tools": turn.get("tools", []),
                "timestamp": turn.get("timestamp"),
            })

        return {
            "metadata": self.metadata(),
            "messages": messages,
            "offset": offset,
            "limit": limit,
            "filtered_count": len(filtered),
            "has_more": (offset + limit) < len(filtered),
        }

    def expand(
        self,
        message_index: int,
        context_window: int = 5,
    ) -> dict[str, Any]:
        turns = self.turns
        if message_index < 0 or message_index >= len(turns):
            raise ValueError(
                f"message_index {message_index} out of range [0, {len(turns) - 1}]"
            )
        start = max(0, message_index - context_window)
        end = min(len(turns), message_index + context_window + 1)

        messages = []
        for idx in range(start, end):
            turn = turns[idx]
            messages.append({
                "index": idx,
                "role": turn["role"],
                "text": turn.get("text", ""),
                "tools": turn.get("tools", []),
                "timestamp": turn.get("timestamp"),
                "is_center": idx == message_index,
            })

        return {
            "center_index": message_index,
            "messages": messages,
            "total_messages": len(turns),
        }

    # -- span mapping -------------------------------------------------------

    def span_to_turns(self, span_index: int) -> tuple[int, int]:
        self._ensure_loaded()
        if span_index not in self._span_map:
            raise ValueError(
                f"span_index {span_index} not in span map "
                f"(valid: 0-{max(self._span_map) if self._span_map else 'none'})"
            )
        return self._span_map[span_index]


# ---------------------------------------------------------------------------
# Gateway handler functions
# ---------------------------------------------------------------------------


def session_get(
    session_id: str,
    offset: int = 0,
    limit: int = 10,
    roles: str | None = None,
    message_types: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Browse messages in a session with pagination and filtering."""
    session = ConversationSession(session_id)
    role_list = [r.strip() for r in roles.split(",") if r.strip()] if roles else None
    type_list = (
        [t.strip() for t in message_types.split(",") if t.strip()]
        if message_types
        else None
    )
    return session.get_messages(
        offset=offset, limit=limit, roles=role_list, message_types=type_list,
        query=query,
    )


def session_expand(
    session_id: str,
    message_index: int,
    context_window: int = 5,
) -> dict[str, Any]:
    """Get full context around a specific message."""
    session = ConversationSession(session_id)
    return session.expand(message_index=message_index, context_window=context_window)


def session_locate(
    session_id: str,
    span_index: int,
) -> dict[str, Any]:
    """Map an IR chunk (from context_search) to a page of messages centered on that chunk.

    Resolves the span to turn indices, runs a sanity check against the IR store,
    and returns a page of messages centered on the span.
    """
    session = ConversationSession(session_id)
    session._ensure_loaded()

    # Resolve span to turn range
    try:
        turn_start, turn_end = session.span_to_turns(span_index)
    except ValueError as exc:
        return {"error": str(exc), "session_id": session.session_id}

    # Center a page around the span
    span_center = (turn_start + turn_end) // 2
    context = 5
    page_start = max(0, span_center - context)
    page_end = min(session.message_count, span_center + context + 1)

    messages = []
    for idx in range(page_start, page_end):
        turn = session.turns[idx]
        in_span = turn_start <= idx < turn_end
        messages.append({
            "index": idx,
            "role": turn["role"],
            "text": turn.get("text", ""),
            "tools": turn.get("tools", []),
            "timestamp": turn.get("timestamp"),
            "in_target_span": in_span,
        })

    result: dict[str, Any] = {
        "metadata": session.metadata(),
        "span_index": span_index,
        "span_turn_range": [turn_start, turn_end],
        "messages": messages,
        "total_messages": session.message_count,
    }

    # Sanity check against IR store
    warning = _check_span_compatibility(session)
    if warning:
        result["warning"] = warning

    return result


def _check_span_compatibility(session: ConversationSession) -> str | None:
    """Compare our replayed span count against what the IR store has indexed.

    Returns a warning string if there's a mismatch, or None if all is well.
    """
    try:
        from work_buddy.ir.store import get_connection, query_documents

        conn = get_connection()
        try:
            indexed = query_documents(
                conn,
                source="conversation",
                doc_id_prefix=f"{session.session_id}:",
            )
        finally:
            conn.close()

        if not indexed:
            return (
                "Session not in IR index. span_index mapping is based on "
                "replayed chunking — results may be approximate if the session "
                "was indexed with different settings."
            )

        indexed_span_count = len(indexed)
        local_span_count = len(session._span_map)
        if indexed_span_count != local_span_count:
            return (
                f"Chunk mismatch: IR has {indexed_span_count} spans, "
                f"replay produced {local_span_count}. The index may be stale "
                f"or config changed — results are approximate."
            )

        return None
    except Exception:
        # Don't let IR store issues block session inspection
        return None


# ---------------------------------------------------------------------------
# session_search — hybrid IR search within a single session
# ---------------------------------------------------------------------------


def build_session_map(days: int = 7) -> dict[str, str]:
    """Return ``{short_hash: full_session_id}`` for all agent commits in the window.

    Designed as the join input for ``git_collector.collect(session_map=...)``.
    Stores the **full** session UUID so consumers can truncate at whatever
    display boundary they choose.  For display annotation,
    ``git_collector._annotate_commits`` truncates to 8 chars — the canonical
    short form accepted by all ``session_*`` capabilities via
    :func:`resolve_session_id`.
    """
    result = session_commits(days=days)
    return {
        c["hash"][:7]: c["session_id"]
        for c in result["commits"]
        if c.get("hash")
    }


def session_commits(
    session_id: str | None = None,
    days: int = 7,
    project: str | None = None,
) -> dict[str, Any]:
    """Extract git commits made during one or all recent sessions.

    Parses raw JSONL entries to find ``git commit`` Bash calls and their
    results.  Uses fast file-level grep, single-pass turn-index tracking,
    per-file mtime caching, and parallel I/O for performance.

    Args:
        session_id: Scope to a single session (overrides *days*/*project*).
        days: How far back to scan (default 7).
        project: Scope to a specific project directory name under
            ``~/.claude/projects/``.  Dramatically reduces scan scope.

    Each commit includes a ``message_index`` field — the turn index of the
    assistant message that issued the ``git commit`` command.
    """
    if session_id:
        paths = [resolve_session_path(session_id)]
    else:
        paths = _recent_sessions(days, project=project)

    all_commits = _extract_commits_parallel(paths)

    # Sort newest first
    all_commits.sort(key=lambda c: c.get("timestamp", ""), reverse=True)

    return {
        "commit_count": len(all_commits),
        "commits": all_commits,
    }


# ---------------------------------------------------------------------------
# Git commit extraction helpers
# ---------------------------------------------------------------------------

_GIT_COMMIT_RE = re.compile(r"git\s+commit\b")
_COMMIT_OUTPUT_RE = re.compile(
    r"\[(\S+)\s+([0-9a-f]{7,40})\]\s+(.+?)(?:\n|$)"
)
_FILES_CHANGED_RE = re.compile(
    r"(\d+)\s+files?\s+changed"
)

# Per-file commit cache: path_str -> (mtime, commits_list)
_commit_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _recent_sessions(
    days: int,
    project: str | None = None,
) -> list[tuple[Path, str]]:
    """Find all non-subagent sessions modified within *days* days.

    If *project* is given, only scan project directories whose resolved
    repo name matches (handles subdirectory sessions correctly).
    """
    import time

    from work_buddy.collectors.chat_collector import project_name_from_slug

    cutoff = time.time() - days * 86400
    results = []
    if not _CLAUDE_PROJECTS.is_dir():
        return results

    if project:
        dirs = [
            d for d in _CLAUDE_PROJECTS.iterdir()
            if d.is_dir() and project_name_from_slug(d.name) == project
        ]
    else:
        dirs = [d for d in _CLAUDE_PROJECTS.iterdir() if d.is_dir()]

    for project_dir in dirs:
        for jsonl in project_dir.glob("*.jsonl"):
            if "subagents" in str(jsonl):
                continue
            if jsonl.stat().st_mtime >= cutoff:
                results.append((jsonl, jsonl.stem))
    return results


def _extract_commits_parallel(
    paths: list[tuple[Path, str]],
) -> list[dict[str, Any]]:
    """Extract commits from multiple session files in parallel.

    Uses three layers of optimization:
    1. **mtime cache** — skip files we've already parsed if unchanged.
    2. **Binary grep** — skip files that don't contain ``git commit``.
    3. **Single-pass extraction** — parse commits AND track turn indices
       in one file read (no second pass).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_commits: list[dict[str, Any]] = []
    work: list[tuple[Path, str]] = []

    for path, sid in paths:
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cached = _commit_cache.get(key)
        if cached and cached[0] == mtime:
            all_commits.extend(cached[1])
        else:
            work.append((path, sid))

    if not work:
        return all_commits

    def _process(path: Path, sid: str) -> list[dict[str, Any]]:
        mtime = path.stat().st_mtime
        # Fast binary grep — skip files with no "git commit" at all
        try:
            with open(path, "rb") as fb:
                if b"git commit" not in fb.read():
                    _commit_cache[str(path)] = (mtime, [])
                    return []
        except OSError:
            return []

        commits = _extract_commits_single_pass(path, sid)
        _commit_cache[str(path)] = (mtime, commits)
        return commits

    with ThreadPoolExecutor(max_workers=min(8, len(work))) as pool:
        futures = {
            pool.submit(_process, path, sid): sid
            for path, sid in work
        }
        for future in as_completed(futures):
            try:
                all_commits.extend(future.result())
            except Exception:
                pass

    return all_commits


def _extract_commits_single_pass(
    path: Path, session_id: str,
) -> list[dict[str, Any]]:
    """Extract git commits from a JSONL file in a single pass.

    Combines commit extraction with turn-index tracking so we never need
    to read the file twice.  The turn counter mirrors the logic of
    ``iter_session_turns`` (skips tool_result entries, isMeta entries,
    empty user entries) to produce correct ``message_index`` values.
    """
    pending: dict[str, dict[str, Any]] = {}  # tool_use_id -> partial commit
    commits: list[dict[str, Any]] = []
    turn_index = 0  # mirrors iter_session_turns enumeration

    with open(path, encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            # Line-level skip: only parse lines that might be relevant
            # to either commit detection or turn counting
            if not raw_line.strip():
                continue

            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")
            timestamp = entry.get("timestamp", "")
            content = entry.get("message", {}).get("content", [])

            # --- Turn counting (mirrors iter_session_turns logic) ---
            if entry_type == "user":
                if entry.get("isMeta"):
                    continue
                if isinstance(content, list):
                    has_tool_result = any(
                        isinstance(c, dict) and c.get("type") == "tool_result"
                        for c in content
                    )
                    if has_tool_result:
                        # Process tool_results for commit output, but don't
                        # increment turn_index (iter_session_turns skips these)
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") != "tool_result":
                                continue
                            tid = block.get("tool_use_id", "")
                            if tid not in pending:
                                continue
                            if block.get("is_error"):
                                pending.pop(tid)
                                continue

                            partial = pending.pop(tid)
                            stdout = entry.get("toolUseResult", {}).get(
                                "stdout", ""
                            )
                            output = stdout or block.get("content", "")

                            m = _COMMIT_OUTPUT_RE.search(output)
                            if not m:
                                continue

                            fc = _FILES_CHANGED_RE.search(output)
                            commits.append({
                                "session_id": partial["session_id"],
                                "hash": m.group(2),
                                "branch": m.group(1),
                                "message": m.group(3).strip(),
                                "files_changed": (
                                    int(fc.group(1)) if fc else None
                                ),
                                "timestamp": partial["timestamp"],
                                "message_index": partial["turn_index"],
                            })
                        continue

                    # Non-tool-result user entry — count as a turn if non-empty
                    text_parts = [
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    if " ".join(text_parts).strip():
                        turn_index += 1
                elif isinstance(content, str) and content.strip():
                    turn_index += 1

            elif entry_type == "assistant":
                if not isinstance(content, list):
                    continue
                has_text = False
                has_tools = False
                current_turn_idx = turn_index

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text" and block.get("text", "").strip():
                        has_text = True
                    elif btype == "tool_use":
                        has_tools = True
                        if block.get("name") == "Bash":
                            cmd = block.get("input", {}).get("command", "")
                            if _GIT_COMMIT_RE.search(cmd):
                                pending[block.get("id", "")] = {
                                    "session_id": session_id,
                                    "command": cmd[:500],
                                    "timestamp": timestamp,
                                    "turn_index": current_turn_idx,
                                }

                if has_text or has_tools:
                    turn_index += 1

    return commits


# ---------------------------------------------------------------------------
# File write extraction helpers
# ---------------------------------------------------------------------------

_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})


def _extract_writes_from_jsonl(path: Path) -> dict[Path, str]:
    """Parse a JSONL session file and return file paths with last-write timestamps.

    Scans assistant ``tool_use`` blocks for Write, Edit, and NotebookEdit calls
    and extracts the absolute file path from each.  No tool_result pairing is
    needed — the target path is in the tool_use input.

    Returns ``{resolved_path: last_write_timestamp}`` where the timestamp is the
    ISO string from the JSONL entry.  If a file is written multiple times within
    a session, only the latest timestamp is kept.

    Failed writes won't appear in ``git status`` anyway, so false positives
    from error'd tool calls self-correct at the intersection step.
    """
    # path -> latest timestamp
    written: dict[Path, str] = {}

    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            content = entry.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            timestamp = entry.get("timestamp", "")

            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                if name not in _WRITE_TOOLS:
                    continue

                inp = block.get("input", {})
                # Write/Edit use "file_path"; NotebookEdit uses "notebook_path"
                fp = inp.get("file_path") or inp.get("notebook_path")
                if not fp:
                    continue
                try:
                    resolved = Path(fp).resolve()
                except (ValueError, OSError):
                    continue
                # Keep the latest write timestamp per file
                if resolved not in written or timestamp > written[resolved]:
                    written[resolved] = timestamp

    return written


def _committed_files_per_session(
    days: int,
    repos_root: Path,
) -> dict[str, set[Path]]:
    """Return ``{session_id: set_of_absolute_paths}`` for files committed by each session.

    Uses :func:`session_commits` to get hashes, then ``git show --name-only``
    to resolve each hash to its touched files.  Only repos under *repos_root*
    are checked.
    """
    import subprocess

    from work_buddy.collectors.git_collector import _discover_repos

    commits = session_commits(days=days)
    if not commits["commits"]:
        return {}

    # Build hash -> session_id mapping
    hash_to_sid: dict[str, str] = {}
    for c in commits["commits"]:
        h = c.get("hash")
        if h:
            hash_to_sid[h] = c["session_id"]

    # Batch: for each repo, resolve hashes to file lists
    result: dict[str, set[Path]] = {}
    all_hashes = set(hash_to_sid.keys())

    for repo_path in _discover_repos(repos_root):
        for h in list(all_hashes):
            try:
                proc = subprocess.run(
                    ["git", "show", "--name-only", "--format=", h],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if proc.returncode != 0:
                    continue
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue

            sid = hash_to_sid[h]
            for fname in proc.stdout.strip().splitlines():
                if fname:
                    try:
                        abs_path = (repo_path / fname).resolve()
                        result.setdefault(sid, set()).add(abs_path)
                    except (ValueError, OSError):
                        continue

    return result


def session_uncommitted(
    days: int = 7,
) -> dict[str, Any]:
    """Find the last agent session that wrote each currently-dirty file.

    Cross-references Write/Edit/NotebookEdit tool calls from recent sessions
    against ``git status --porcelain`` across all repos under ``repos_root``.

    For each dirty file, only the **most recent session** to write it is
    reported — earlier sessions' edits have been overwritten and are not
    actionable.  Sessions that wrote a file and later committed it are
    excluded entirely.

    Returns structured report grouped by session: each entry lists the
    dirty files that session is responsible for.
    """
    from work_buddy.collectors.git_collector import _discover_repos, _get_status
    from work_buddy.config import load_config

    cfg = load_config()
    repos_root = Path(cfg["repos_root"])

    # --- Phase 1: find the last writer of every file across all sessions ---
    # last_writer: {abs_path: (session_id, timestamp)}
    last_writer: dict[Path, tuple[str, str]] = {}
    session_paths = _recent_sessions(days)
    sessions_with_writes = set()

    for path, sid in session_paths:
        writes = _extract_writes_from_jsonl(path)
        if not writes:
            continue
        sessions_with_writes.add(sid)
        for file_path, timestamp in writes.items():
            prev = last_writer.get(file_path)
            if prev is None or timestamp > prev[1]:
                last_writer[file_path] = (sid, timestamp)

    if not last_writer:
        return {
            "uncommitted_count": 0,
            "uncommitted": [],
            "clean_sessions": 0,
            "scanned_sessions": 0,
        }

    # --- Phase 1b: remove files the last-writer session already committed ---
    committed_map = _committed_files_per_session(days, repos_root)
    for file_path, (sid, _ts) in list(last_writer.items()):
        committed = committed_map.get(sid, set())
        if file_path in committed:
            del last_writer[file_path]

    # --- Phase 2: collect dirty files across all repos ---
    dirty_files: dict[Path, tuple[str, str]] = {}  # abs_path -> (repo_name, status)
    for repo_path in _discover_repos(repos_root):
        raw_status = _get_status(repo_path)
        if not raw_status:
            continue
        for line in raw_status.splitlines():
            if len(line) < 4:
                continue
            status_code = line[:2].strip()
            rel_path = line[3:]
            # Handle renames: "old -> new" format
            if " -> " in rel_path:
                rel_path = rel_path.split(" -> ")[-1]
            try:
                abs_path = (repo_path / rel_path).resolve()
                dirty_files[abs_path] = (repo_path.name, status_code)
            except (ValueError, OSError):
                continue

    # --- Phase 3: intersect and group by session ---
    # Only keep files that are both: last-written by a session AND currently dirty
    by_session: dict[str, dict[str, list[dict[str, str]]]] = {}
    blamed_sessions: set[str] = set()

    for file_path, (sid, _ts) in last_writer.items():
        if file_path not in dirty_files:
            continue
        repo_name, status_code = dirty_files[file_path]
        blamed_sessions.add(sid)
        by_session.setdefault(sid, {}).setdefault(repo_name, []).append({
            "file": file_path.name,
            "path": file_path.as_posix(),
            "status": status_code,
        })

    uncommitted: list[dict[str, Any]] = []
    for sid in sorted(by_session):
        for repo_name, files in sorted(by_session[sid].items()):
            files.sort(key=lambda f: f["file"])
            uncommitted.append({
                "session_id": sid,
                "repo": repo_name,
                "files": [f["file"] for f in files],
                "paths": [f["path"] for f in files],
                "status": [f["status"] for f in files],
            })

    clean_count = len(sessions_with_writes - blamed_sessions)

    return {
        "uncommitted_count": len(uncommitted),
        "uncommitted": uncommitted,
        "clean_sessions": clean_count,
        "scanned_sessions": len(sessions_with_writes),
    }


def session_search(
    session_id: str,
    query: str,
    method: str = "keyword,semantic",
    top_k: int = 5,
) -> dict[str, Any]:
    """Hybrid IR search within a single session, resolved to message-level results.

    Uses the IR search system (keyword, semantic, or substring) scoped to
    a single session, then resolves chunk-level hits to message indices
    via the span-to-turn mapping.
    """
    from work_buddy.ir.search import search as ir_search

    session = ConversationSession(session_id)
    session._ensure_loaded()

    # Search IR scoped to this session
    results = ir_search(
        query,
        source="conversation",
        scope=session.session_id,
        method=method,
        top_k=top_k,
    )
    if isinstance(results, str):
        return {"error": results}

    # Resolve each chunk hit to message indices via span map
    message_hits = []
    for hit in results:
        span_idx = hit.get("metadata", {}).get("span_index")
        if span_idx is None:
            continue
        try:
            turn_start, turn_end = session.span_to_turns(span_idx)
        except ValueError:
            continue

        span_messages = []
        for idx in range(turn_start, turn_end):
            turn = session.turns[idx]
            span_messages.append({
                "index": idx,
                "role": turn["role"],
                "text": turn.get("text", ""),
                "tools": turn.get("tools", []),
                "timestamp": turn.get("timestamp"),
            })

        message_hits.append({
            "span_index": span_idx,
            "score": hit["score"],
            "turn_range": [turn_start, turn_end],
            "messages": span_messages,
        })

    result: dict[str, Any] = {
        "metadata": session.metadata(),
        "query": query,
        "method": method,
        "hits": message_hits,
    }

    warning = _check_span_compatibility(session)
    if warning:
        result["warning"] = warning

    return result


def session_wb_activity(
    session_id: str | None = None,
    agent_session_id: str | None = None,
) -> dict[str, Any]:
    """Get a summary of what a session did through work-buddy's MCP gateway.

    Reads from the session's activity ledger. If *session_id* is None,
    returns activity for the current session. The *agent_session_id*
    parameter is an alias automatically injected by the gateway.
    """
    sid = session_id or agent_session_id
    from work_buddy.mcp_server.activity_ledger import (
        query_activity,
        query_session_summary,
    )

    summary = query_session_summary(agent_session_id=sid)
    recent = query_activity(last_n=20, agent_session_id=sid)

    return {
        "session_id": summary.get("session_id"),
        "total_events": summary.get("total_events", 0),
        "duration_minutes": summary.get("duration_minutes"),
        "by_category": summary.get("by_category", {}),
        "by_capability": summary.get("by_capability", {}),
        "workflows_started": summary.get("workflows_started", 0),
        "workflows_completed": summary.get("workflows_completed", 0),
        "errors": summary.get("errors", 0),
        "consent_requests": summary.get("consent_requests", 0),
        "mutations": summary.get("mutations", 0),
        "key_artifacts": summary.get("key_artifacts", []),
        "recent_events": [
            {
                "ts": ev.get("ts"),
                "type": ev.get("type"),
                "capability": ev.get("capability"),
                "status": ev.get("status"),
            }
            for ev in recent.get("events", [])[:10]
        ],
    }
