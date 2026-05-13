"""Durable session-attributed file-write storage and refresh.

Mirrors ``commits.py``: replaces the ad-hoc per-call scan in
``sessions/inspector.session_uncommitted`` with a SQLite-backed cache.
Each Write/Edit/NotebookEdit tool call across recent sessions becomes a
durable row keyed by ``(session_id, file_path)``; ``session_uncommitted``
becomes a read-after-refresh that the journal/context bundle can use
without re-scanning every JSONL.

Public surface:

* :func:`refresh_session_writes` — scan recent sessions, upsert write
  rows, cross-reference against ``session_commits`` and current
  ``git status --porcelain`` to populate ``committed_sha`` and
  ``currently_dirty``.
* :func:`query_session_writes` — read-only DB query.
* :func:`uncommitted_report` — produces the legacy
  :func:`sessions.inspector.session_uncommitted` shape from the DB.

``currently_dirty`` is recomputed every refresh because git state is
mutable. The stored flag is a snapshot — best-effort, not authoritative.
The refresh is the moment of truth; query callers should consult the
freshest refresh before acting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.conversation_observability.commits import (
    refresh_session_commits,
)
from work_buddy.conversation_observability.db import get_connection


# ---------------------------------------------------------------------------
# Refresh — scan, persist, cross-reference against git state
# ---------------------------------------------------------------------------


def refresh_session_writes(
    days: int = 7,
    repos_root: Path | None = None,
) -> dict[str, Any]:
    """Scan recent JSONL sessions and update the write-attribution table.

    Workflow:
      1. Refresh commits first so ``committed_sha`` lookups are accurate.
      2. Scan recent JSONL files for Write/Edit/NotebookEdit tool calls.
      3. For each (session_id, file_path) write, upsert one row carrying
         the latest write timestamp and the originating tool name.
      4. Collect dirty files from all repos under ``repos_root``.
      5. Recompute ``currently_dirty`` and ``committed_sha`` for every
         row.

    Returns a summary dict (``{rows_written, dirty_rows, scanned_sessions}``)
    for observability — callers wanting the legacy "by session" report
    should use :func:`uncommitted_report`.
    """
    from work_buddy.collectors.git_collector import _discover_repos, _get_status
    from work_buddy.config import load_config
    from work_buddy.sessions.inspector import (
        _WRITE_TOOLS,
        _extract_writes_from_jsonl,
        _recent_sessions,
    )

    if repos_root is None:
        repos_root = Path(load_config()["repos_root"])

    # 1. Make sure commit attribution is fresh — committed_sha derives
    #    from session_commits rows.
    refresh_session_commits(days=days)

    # 2. Scan recent sessions and gather (sid, path) → (timestamp, tool).
    #    Stale-only: skip files whose ``writes_scanned_mtime`` ledger
    #    column matches the on-disk mtime.
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        session_paths = _recent_sessions(days)
        sessions_scanned: set[str] = set()
        # {(sid, resolved_path): (timestamp, tool_name)}
        writes_by_key: dict[tuple[str, str], tuple[str, str]] = {}
        for jsonl_path, sid in session_paths:
            try:
                file_mtime = jsonl_path.stat().st_mtime
            except OSError:
                continue
            row = conn.execute(
                "SELECT writes_scanned_mtime FROM observed_sessions "
                "WHERE session_id = ?",
                (sid,),
            ).fetchone()
            if (
                row is not None
                and row["writes_scanned_mtime"] is not None
                and abs(row["writes_scanned_mtime"] - file_mtime) < 1e-6
            ):
                # Already scanned at this mtime — leave existing rows
                # in place; dirty-state recompute below will still run.
                sessions_scanned.add(sid)
                continue

            per_file = _extract_writes_from_jsonl(jsonl_path)
            tool_by_path = _scan_tool_names(jsonl_path)
            sessions_scanned.add(sid)
            for path, ts in per_file.items():
                tool = tool_by_path.get(path, "Write")
                writes_by_key[(sid, path.as_posix())] = (ts, tool)

            # Drop stale rows for sessions we re-scanned.
            conn.execute(
                "DELETE FROM session_file_writes WHERE session_id = ?", (sid,)
            )
            # Stamp the per-concern ledger column.
            conn.execute(
                "INSERT INTO observed_sessions "
                "(session_id, source_path, source_mtime, observed_at, "
                " writes_scanned_mtime) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                " source_path=excluded.source_path, "
                " writes_scanned_mtime=excluded.writes_scanned_mtime",
                (sid, str(jsonl_path), file_mtime, now_iso, file_mtime),
            )

        for (sid, fpath), (ts, tool) in writes_by_key.items():
            conn.execute(
                "INSERT INTO session_file_writes "
                "(id, session_id, file_path, tool_name, write_timestamp, "
                " currently_dirty, observed_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (
                    f"{sid}:{fpath}",
                    sid,
                    fpath,
                    tool,
                    ts,
                    now_iso,
                ),
            )
        conn.commit()

        # 4. Get dirty files from all repos.
        dirty: set[str] = set()
        for repo_path in _discover_repos(repos_root):
            raw = _get_status(repo_path)
            if not raw:
                continue
            for line in raw.splitlines():
                if len(line) < 4:
                    continue
                rel_path = line[3:]
                if " -> " in rel_path:
                    rel_path = rel_path.split(" -> ")[-1]
                try:
                    dirty.add((repo_path / rel_path).resolve().as_posix())
                except (ValueError, OSError):
                    continue

        # 5. Update currently_dirty + committed_sha.
        rows = conn.execute(
            "SELECT id, session_id, file_path FROM session_file_writes"
        ).fetchall()
        dirty_count = 0
        for r in rows:
            is_dirty = 1 if r["file_path"] in dirty else 0
            committed_sha = _resolve_committed_sha(
                conn, r["session_id"], r["file_path"], repos_root,
            )
            conn.execute(
                "UPDATE session_file_writes "
                "SET currently_dirty = ?, committed_sha = ? "
                "WHERE id = ?",
                (is_dirty, committed_sha, r["id"]),
            )
            if is_dirty:
                dirty_count += 1
        conn.commit()
    finally:
        conn.close()

    return {
        "rows_written": len(writes_by_key),
        "dirty_rows": dirty_count,
        "scanned_sessions": len(sessions_scanned),
    }


# ---------------------------------------------------------------------------
# Read-only queries
# ---------------------------------------------------------------------------


def query_session_writes(session_id: str | None = None) -> list[dict[str, Any]]:
    """Return write rows from the DB, optionally scoped to one session."""
    conn = get_connection()
    try:
        if session_id is None:
            rows = conn.execute(
                "SELECT * FROM session_file_writes ORDER BY write_timestamp DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM session_file_writes WHERE session_id = ? "
                "ORDER BY write_timestamp DESC",
                (session_id,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def uncommitted_report(
    days: int = 7,
    repos_root: Path | None = None,
) -> dict[str, Any]:
    """Produce the legacy ``session_uncommitted`` shape from the DB.

    Returns ``{uncommitted_count, uncommitted: [...], clean_sessions,
    scanned_sessions}``. Triggers a refresh first so the report is
    fresh. Re-queries ``git status --porcelain`` during assembly so the
    porcelain status code (M, ??, etc.) reflects current state — git
    state is mutable, so storing the code at refresh time would lie.

    Within each session, only the file paths it is the **last writer**
    of are surfaced — earlier sessions whose writes got overwritten by
    a later session are dropped, matching the legacy behavior.
    """
    from work_buddy.collectors.git_collector import _discover_repos, _get_status
    from work_buddy.config import load_config

    if repos_root is None:
        repos_root = Path(load_config()["repos_root"])

    summary = refresh_session_writes(days=days, repos_root=repos_root)

    # Re-collect porcelain status with codes for the report.
    dirty_with_status: dict[str, tuple[str, str]] = {}  # path -> (repo, code)
    for repo_path in _discover_repos(repos_root):
        raw = _get_status(repo_path)
        if not raw:
            continue
        for line in raw.splitlines():
            if len(line) < 4:
                continue
            status_code = line[:2].strip()
            rel_path = line[3:]
            if " -> " in rel_path:
                rel_path = rel_path.split(" -> ")[-1]
            try:
                abs_path = (repo_path / rel_path).resolve().as_posix()
            except (ValueError, OSError):
                continue
            dirty_with_status[abs_path] = (repo_path.name, status_code)

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM session_file_writes "
            "WHERE currently_dirty = 1 AND committed_sha IS NULL "
            "ORDER BY write_timestamp DESC"
        ).fetchall()
    finally:
        conn.close()

    # Keep only the most recent writer of each file path.
    seen_files: dict[str, dict[str, Any]] = {}
    for r in rows:
        path = r["file_path"]
        if path in seen_files:
            continue
        seen_files[path] = dict(r)

    # Group by session, then by repo.
    by_session: dict[str, dict[str, list[dict[str, str]]]] = {}
    blamed: set[str] = set()
    for r in seen_files.values():
        path_obj = Path(r["file_path"])
        info = dirty_with_status.get(path_obj.as_posix())
        if info is None:
            # File was dirty at refresh time but isn't anymore — drop it
            # rather than make up a status code.
            continue
        repo_name, status_code = info
        sid = r["session_id"]
        blamed.add(sid)
        by_session.setdefault(sid, {}).setdefault(repo_name, []).append({
            "file": path_obj.name,
            "path": path_obj.as_posix(),
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

    clean_count = max(0, summary["scanned_sessions"] - len(blamed))
    return {
        "uncommitted_count": len(uncommitted),
        "uncommitted": uncommitted,
        "clean_sessions": clean_count,
        "scanned_sessions": summary["scanned_sessions"],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scan_tool_names(path: Path) -> dict[Path, str]:
    """Map ``{resolved_path: last_tool_used}`` for Write/Edit/NotebookEdit calls.

    The inspector's ``_extract_writes_from_jsonl`` drops the tool name.
    We re-walk the JSONL to recover it. Latest-write wins.
    """
    import json

    from work_buddy.sessions.inspector import _WRITE_TOOLS

    result: dict[Path, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = entry.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "")
                    if name not in _WRITE_TOOLS:
                        continue
                    inp = block.get("input", {})
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if not fp:
                        continue
                    try:
                        resolved = Path(fp).resolve()
                    except (ValueError, OSError):
                        continue
                    # Last tool to write each file wins — matches the
                    # latest-timestamp semantics of the existing
                    # _extract_writes_from_jsonl.
                    result[resolved] = name
    except OSError:
        pass
    return result


def _resolve_committed_sha(
    conn: Any,
    session_id: str,
    file_path: str,
    repos_root: Path,
) -> str | None:
    """Return the sha of a commit by ``session_id`` that touched ``file_path``.

    Uses the cached session_commits rows plus ``git show --name-only``.
    Returns None when no matching commit is found.
    """
    import subprocess

    from work_buddy.collectors.git_collector import _discover_repos

    shas = [
        r["sha"]
        for r in conn.execute(
            "SELECT sha FROM session_commits WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    ]
    if not shas:
        return None

    # Cheap pre-check: file path must contain a repo under repos_root.
    fp = Path(file_path)
    for repo_path in _discover_repos(repos_root):
        try:
            rel = fp.relative_to(repo_path)
        except ValueError:
            continue
        for sha in shas:
            try:
                proc = subprocess.run(
                    ["git", "show", "--name-only", "--format=", sha],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue
            if proc.returncode != 0:
                continue
            for fname in proc.stdout.strip().splitlines():
                if fname and Path(fname) == rel:
                    return sha
        break
    return None


def _infer_repo_name(path: Path) -> str:
    """Best-effort repo name from an absolute path.

    The DB doesn't store the repo name (paths are absolute). We walk
    up looking for a ``.git`` sibling; if not found, fall back to the
    parent directory's name. Used only for the report's grouping
    surface.
    """
    p = path.parent
    for _ in range(20):
        if (p / ".git").exists():
            return p.name
        if p.parent == p:
            break
        p = p.parent
    return path.parent.name or "unknown"
