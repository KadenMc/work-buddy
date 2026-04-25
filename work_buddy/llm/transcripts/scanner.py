"""Scan Claude Code JSONL transcript files into a SQLite cache.

Vendored from `phuryn/claude-usage <https://github.com/phuryn/claude-usage>`_
(MIT, Pawel Huryn, April 2026). Adapted for work-buddy:

* The DB lives at ``data/cache/claude_transcripts.db``
  (registered as ``cache/claude-transcripts``).
* The transcripts root defaults to ``~/.claude/projects/`` and the
  Xcode location, but ``llm.transcripts.projects_dirs`` in
  ``config.yaml`` can override.
* ``print()`` calls are routed through the standard logger.
* Public functions retain their upstream names so a future re-vendor
  diff stays small.

The deduplication trick — keying turns by ``message.id`` and keeping
the **last** record per id — is the key correctness fix that
distinguishes this scanner from naive line counting (Anthropic streams
multiple JSONL records per API response, and only the last has the
final usage tallies).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Higher number = higher priority when choosing a session's primary model.
MODEL_PRIORITY = {"opus": 3, "sonnet": 2, "haiku": 1}


# --- Path resolution -------------------------------------------------------


def _default_projects_dirs() -> list[Path]:
    """Locations Claude Code is known to use for transcript JSONLs."""
    home = Path.home()
    return [
        home / ".claude" / "projects",
        home / "Library" / "Developer" / "Xcode"
            / "CodingAssistant" / "ClaudeAgentConfig" / "projects",
    ]


def get_projects_dirs() -> list[Path]:
    """Return the configured (or default) list of transcript roots."""
    try:
        from work_buddy.config import load_config
        cfg = load_config()
        configured = (cfg.get("llm", {}) or {}).get("transcripts", {}).get("projects_dirs")
        if configured:
            return [Path(p).expanduser() for p in configured]
    except Exception:
        pass
    return _default_projects_dirs()


def get_db_path() -> Path:
    """Resolve the SQLite cache location through ``work_buddy.paths``."""
    from work_buddy.paths import resolve
    return resolve("cache/claude-transcripts")


# --- DB ---------------------------------------------------------------------


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the cache DB, creating its parent if needed."""
    p = db_path or get_db_path()
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if missing."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            tool_name               TEXT,
            cwd                     TEXT,
            message_id              TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
    """)
    try:
        conn.execute("SELECT message_id FROM turns LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE turns ADD COLUMN message_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message_id "
        "ON turns(message_id) WHERE message_id IS NOT NULL AND message_id != ''"
    )
    conn.commit()


# --- Helpers ----------------------------------------------------------------


def _model_priority(model: str | None) -> int:
    if not model:
        return 0
    m = model.lower()
    for keyword, priority in MODEL_PRIORITY.items():
        if keyword in m:
            return priority
    return 0


def project_name_from_cwd(cwd: str | None) -> str:
    """Last two path components — a friendly project label."""
    if not cwd:
        return "unknown"
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


# --- Single-file parser -----------------------------------------------------


def parse_jsonl_file(filepath: str) -> tuple[list[dict[str, Any]],
                                              list[dict[str, Any]],
                                              int]:
    """Parse one JSONL file into ``(session_metas, turns, line_count)``.

    Streaming events are deduplicated by ``message.id`` — only the last
    record per id wins.
    """
    seen_messages: dict[str, dict[str, Any]] = {}
    turns_no_id: list[dict[str, Any]] = []
    session_meta: dict[str, dict[str, Any]] = {}
    line_count = 0

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line_count, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rtype = record.get("type")
                if rtype not in ("assistant", "user"):
                    continue

                session_id = record.get("sessionId")
                if not session_id:
                    continue

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch", "")

                meta = session_meta.get(session_id)
                if meta is None:
                    session_meta[session_id] = {
                        "session_id": session_id,
                        "project_name": project_name_from_cwd(cwd),
                        "first_timestamp": timestamp,
                        "last_timestamp": timestamp,
                        "git_branch": git_branch,
                        "model": None,
                    }
                else:
                    if timestamp and (not meta["first_timestamp"]
                                      or timestamp < meta["first_timestamp"]):
                        meta["first_timestamp"] = timestamp
                    if timestamp and (not meta["last_timestamp"]
                                      or timestamp > meta["last_timestamp"]):
                        meta["last_timestamp"] = timestamp
                    if git_branch and not meta["git_branch"]:
                        meta["git_branch"] = git_branch

                if rtype != "assistant":
                    continue

                msg = record.get("message", {})
                usage = msg.get("usage", {}) or {}
                model = msg.get("model", "")
                message_id = msg.get("id", "")

                input_tokens = usage.get("input_tokens", 0) or 0
                output_tokens = usage.get("output_tokens", 0) or 0
                cache_read = usage.get("cache_read_input_tokens", 0) or 0
                cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

                if (input_tokens + output_tokens
                        + cache_read + cache_creation == 0):
                    continue

                tool_name = None
                for item in msg.get("content", []):
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        tool_name = item.get("name")
                        break

                if model:
                    session_meta[session_id]["model"] = model

                turn = {
                    "session_id": session_id,
                    "timestamp": timestamp,
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read,
                    "cache_creation_tokens": cache_creation,
                    "tool_name": tool_name,
                    "cwd": cwd,
                    "message_id": message_id,
                }
                if message_id:
                    seen_messages[message_id] = turn
                else:
                    turns_no_id.append(turn)
    except OSError as exc:
        logger.warning("transcripts: error reading %s: %s", filepath, exc)

    turns = turns_no_id + list(seen_messages.values())
    return list(session_meta.values()), turns, line_count


def aggregate_sessions(
    session_metas: list[dict[str, Any]],
    turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Roll turns up to per-session token / turn-count totals."""
    session_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "turn_count": 0,
        "model": None,
    })
    session_model_counts: dict[str, Counter] = defaultdict(Counter)

    for t in turns:
        s = session_stats[t["session_id"]]
        s["total_input_tokens"] += t["input_tokens"]
        s["total_output_tokens"] += t["output_tokens"]
        s["total_cache_read"] += t["cache_read_tokens"]
        s["total_cache_creation"] += t["cache_creation_tokens"]
        s["turn_count"] += 1
        if t["model"]:
            session_model_counts[t["session_id"]][t["model"]] += 1

    for sid, counts in session_model_counts.items():
        if counts:
            session_stats[sid]["model"] = counts.most_common(1)[0][0]

    result: list[dict[str, Any]] = []
    for meta in session_metas:
        sid = meta["session_id"]
        stats = session_stats[sid]
        result.append({**meta, **stats})
    return result


# --- Persistence ------------------------------------------------------------


def upsert_sessions(conn: sqlite3.Connection,
                    sessions: list[dict[str, Any]]) -> None:
    for s in sessions:
        existing = conn.execute(
            "SELECT total_input_tokens FROM sessions WHERE session_id = ?",
            (s["session_id"],),
        ).fetchone()
        if existing is None:
            conn.execute("""
                INSERT INTO sessions
                    (session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, model, turn_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["session_id"], s["project_name"], s["first_timestamp"],
                s["last_timestamp"], s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["model"], s["turn_count"],
            ))
        else:
            existing_model = conn.execute(
                "SELECT model FROM sessions WHERE session_id = ?",
                (s["session_id"],),
            ).fetchone()["model"]
            new_model = s["model"]
            model_to_set = (
                new_model
                if _model_priority(new_model) > _model_priority(existing_model)
                else existing_model
            )
            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read = total_cache_read + ?,
                    total_cache_creation = total_cache_creation + ?,
                    turn_count = turn_count + ?,
                    model = ?
                WHERE session_id = ?
            """, (
                s["last_timestamp"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["turn_count"], model_to_set,
                s["session_id"],
            ))


def insert_turns(conn: sqlite3.Connection,
                 turns: list[dict[str, Any]]) -> None:
    conn.executemany("""
        INSERT OR IGNORE INTO turns
            (session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (t["session_id"], t["timestamp"], t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t["tool_name"], t["cwd"], t.get("message_id", ""))
        for t in turns
    ])


# --- Driver ----------------------------------------------------------------


def scan(
    *,
    projects_dirs: list[Path] | None = None,
    db_path: Path | None = None,
    full_rebuild: bool = False,
) -> dict[str, Any]:
    """Run a (potentially incremental) scan over Claude Code transcripts.

    Args:
        projects_dirs: Override the configured / default transcript
            roots. Useful for tests.
        db_path: Override the cache DB location. Useful for tests.
        full_rebuild: When ``True``, delete the existing DB before
            scanning so token totals don't double-count if you change
            schema or pricing assumptions.

    Returns the same shape as upstream ``claude-usage`` plus the
    resolved ``db_path`` for clients that need to know where the data
    landed.
    """
    if db_path is None:
        db_path = get_db_path()
    if full_rebuild and db_path.exists():
        try:
            db_path.unlink()
        except OSError as exc:
            logger.warning("transcripts: failed to remove db %s: %s",
                           db_path, exc)

    conn = get_db(db_path)
    init_db(conn)

    dirs_to_scan = (
        [Path(d) for d in projects_dirs] if projects_dirs
        else get_projects_dirs()
    )

    jsonl_files: list[str] = []
    for d in dirs_to_scan:
        if not d.exists():
            continue
        jsonl_files.extend(
            glob.glob(str(d / "**" / "*.jsonl"), recursive=True))
    jsonl_files.sort()

    new_files = 0
    updated_files = 0
    skipped_files = 0
    total_turns = 0
    total_sessions: set[str] = set()

    for filepath in jsonl_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ?",
            (filepath,),
        ).fetchone()

        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            continue

        is_new = row is None
        if is_new:
            session_metas, turns, line_count = parse_jsonl_file(filepath)
            if turns or session_metas:
                sessions = aggregate_sessions(session_metas, turns)
                upsert_sessions(conn, sessions)
                insert_turns(conn, turns)
                for s in sessions:
                    total_sessions.add(s["session_id"])
                total_turns += len(turns)
                new_files += 1
        else:
            old_lines = row["lines"] if row else 0
            seen_messages: dict[str, dict[str, Any]] = {}
            turns_no_id: list[dict[str, Any]] = []
            new_session_metas: dict[str, dict[str, Any]] = {}
            line_count = 0
            try:
                with open(filepath, encoding="utf-8", errors="replace") as f:
                    for line_count, line in enumerate(f, 1):
                        if line_count <= old_lines:
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        rtype = record.get("type")
                        if rtype not in ("assistant", "user"):
                            continue
                        session_id = record.get("sessionId")
                        if not session_id:
                            continue
                        timestamp = record.get("timestamp", "")
                        cwd = record.get("cwd", "")

                        meta = new_session_metas.get(session_id)
                        if meta is None:
                            new_session_metas[session_id] = {
                                "session_id": session_id,
                                "project_name": project_name_from_cwd(cwd),
                                "first_timestamp": timestamp,
                                "last_timestamp": timestamp,
                                "git_branch": record.get("gitBranch", ""),
                                "model": None,
                            }
                        else:
                            if timestamp and (not meta["last_timestamp"]
                                              or timestamp > meta["last_timestamp"]):
                                meta["last_timestamp"] = timestamp

                        if rtype != "assistant":
                            continue

                        msg = record.get("message", {})
                        usage = msg.get("usage", {}) or {}
                        model = msg.get("model", "")
                        message_id = msg.get("id", "")
                        input_tokens = usage.get("input_tokens", 0) or 0
                        output_tokens = usage.get("output_tokens", 0) or 0
                        cache_read = usage.get("cache_read_input_tokens", 0) or 0
                        cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                        if (input_tokens + output_tokens + cache_read
                                + cache_creation == 0):
                            continue
                        tool_name = None
                        for item in msg.get("content", []):
                            if isinstance(item, dict) and item.get("type") == "tool_use":
                                tool_name = item.get("name")
                                break
                        if model:
                            new_session_metas[session_id]["model"] = model
                        turn = {
                            "session_id": session_id,
                            "timestamp": timestamp,
                            "model": model,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "cache_read_tokens": cache_read,
                            "cache_creation_tokens": cache_creation,
                            "tool_name": tool_name,
                            "cwd": cwd,
                            "message_id": message_id,
                        }
                        if message_id:
                            seen_messages[message_id] = turn
                        else:
                            turns_no_id.append(turn)
            except OSError as exc:
                logger.warning("transcripts: error reading %s: %s",
                               filepath, exc)

            if line_count <= old_lines:
                conn.execute(
                    "UPDATE processed_files SET mtime = ? WHERE path = ?",
                    (mtime, filepath),
                )
                conn.commit()
                skipped_files += 1
                continue

            new_turns = turns_no_id + list(seen_messages.values())
            if new_turns or new_session_metas:
                sessions = aggregate_sessions(
                    list(new_session_metas.values()), new_turns)
                upsert_sessions(conn, sessions)
                insert_turns(conn, new_turns)
                for s in sessions:
                    total_sessions.add(s["session_id"])
                total_turns += len(new_turns)
            updated_files += 1

        conn.execute("""
            INSERT OR REPLACE INTO processed_files (path, mtime, lines)
            VALUES (?, ?, ?)
        """, (filepath, mtime, line_count))
        conn.commit()

    if new_files or updated_files:
        # Recompute session totals from the actual turn rows so
        # ``INSERT OR IGNORE`` doesn't leave token totals overcounted
        # when a duplicate message_id was rejected.
        conn.execute("""
            UPDATE sessions SET
                total_input_tokens = COALESCE((SELECT SUM(input_tokens)
                    FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_output_tokens = COALESCE((SELECT SUM(output_tokens)
                    FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_cache_read = COALESCE((SELECT SUM(cache_read_tokens)
                    FROM turns WHERE turns.session_id = sessions.session_id), 0),
                total_cache_creation = COALESCE((SELECT SUM(cache_creation_tokens)
                    FROM turns WHERE turns.session_id = sessions.session_id), 0),
                turn_count = COALESCE((SELECT COUNT(*)
                    FROM turns WHERE turns.session_id = sessions.session_id), 0)
        """)
        conn.commit()

    conn.close()
    return {
        "new": new_files,
        "updated": updated_files,
        "skipped": skipped_files,
        "turns": total_turns,
        "sessions": len(total_sessions),
        "db_path": str(db_path),
    }
