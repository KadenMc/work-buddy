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
    max_sessions: int | None = None,
) -> dict[str, Any]:
    """Scan recent JSONL sessions and update the write-attribution table.

    Workflow:
      1. Refresh commits first so the report side can join against fresh
         session_commits rows. (No committed_sha resolution here —
         that runs lazily in :func:`uncommitted_report`, deduped per
         SHA, scoped to dirty rows only.)
      2. Scan recent JSONL files for Write/Edit/NotebookEdit tool calls.
      3. For each (session_id, file_path) write, upsert one row carrying
         the latest write timestamp and the originating tool name.
      4. Collect dirty files from all repos under ``repos_root``.
      5. Recompute ``currently_dirty`` for every row.

    Returns a summary dict (``{rows_written, dirty_rows, scanned_sessions}``)
    for observability — callers wanting the legacy "by session" report
    should use :func:`uncommitted_report`.

    ``max_sessions`` caps the number of cache MISSES per call so a
    cold-start scan splits across multiple cron firings instead of
    running for many minutes. Cache hits and the dirty-state recompute
    pass are unbounded since both are cheap.
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

    # 1. Make sure commit attribution is fresh — uncommitted_report
    #    joins against session_commits rows when filtering out files
    #    a session also committed.
    refresh_session_commits(days=days, max_sessions=max_sessions)

    # 2. Pre-pass: pull the scan ledger so the JSONL loop doesn't hold
    #    a DB connection open. Stale-only skip uses the
    #    ``writes_scanned_mtime`` column owned by this refresher.
    now_iso = datetime.now(timezone.utc).isoformat()
    session_paths = _recent_sessions(days)
    conn = get_connection()
    try:
        ledger_rows = conn.execute(
            "SELECT session_id, writes_scanned_mtime FROM observed_sessions"
        ).fetchall()
    finally:
        conn.close()
    writes_scanned_at = {
        r["session_id"]: r["writes_scanned_mtime"] for r in ledger_rows
    }

    # 3. JSONL scan loop — parse OUTSIDE the connection, then write the
    #    per-session row batch + ledger stamp in a short transaction.
    sessions_scanned: set[str] = set()
    rows_written = 0
    misses = 0
    for jsonl_path, sid in session_paths:
        try:
            file_mtime = jsonl_path.stat().st_mtime
        except OSError:
            continue

        cached = writes_scanned_at.get(sid)
        if cached is not None and abs(cached - file_mtime) < 1e-6:
            sessions_scanned.add(sid)
            continue

        # Cache MISS: bounded by ``max_sessions``.
        if max_sessions is not None and misses >= max_sessions:
            continue
        misses += 1

        per_file = _extract_writes_from_jsonl(jsonl_path)
        tool_by_path = _scan_tool_names(jsonl_path)
        sessions_scanned.add(sid)
        session_writes: list[tuple[str, str]] = []  # [(path_str, tool)]
        for fp, ts in per_file.items():
            tool = tool_by_path.get(fp, "Write")
            session_writes.append((fp.as_posix(), ts, tool))

        conn = get_connection()
        try:
            conn.execute(
                "DELETE FROM session_file_writes WHERE session_id = ?", (sid,)
            )
            for fpath, ts, tool in session_writes:
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
            conn.commit()
        finally:
            conn.close()
        rows_written += len(session_writes)

    # 4. Get dirty files from all repos. Pure subprocess work, no DB.
    #    Parallelize: ``git status`` on every repo serially scales as
    #    O(repos × per-repo-cost) — a 20-repo vault is ~60s sequential
    #    but ~8s with a small thread pool, and the calls are pure I/O
    #    so the GIL is irrelevant.
    from concurrent.futures import ThreadPoolExecutor

    repo_list = list(_discover_repos(repos_root))
    dirty: set[str] = set()

    def _scan_repo(repo_path: Path) -> list[str]:
        raw = _get_status(repo_path)
        out: list[str] = []
        if not raw:
            return out
        for line in raw.splitlines():
            if len(line) < 4:
                continue
            rel_path = line[3:]
            if " -> " in rel_path:
                rel_path = rel_path.split(" -> ")[-1]
            try:
                out.append((repo_path / rel_path).resolve().as_posix())
            except (ValueError, OSError):
                continue
        return out

    if repo_list:
        with ThreadPoolExecutor(max_workers=min(8, len(repo_list))) as pool:
            for paths_in_repo in pool.map(_scan_repo, repo_list):
                dirty.update(paths_in_repo)

    # 5. Recompute currently_dirty for every row. ``committed_sha`` is
    #    intentionally NOT resolved here — running ``git show
    #    --name-only`` for every (row, repo, session-sha) combination
    #    can fan out into thousands of subprocess calls on a large vault
    #    and hang the cron for minutes. ``uncommitted_report`` resolves
    #    it lazily, deduped per SHA, scoped to dirty rows only.
    #
    #    All UPDATEs run in one short transaction. The previous
    #    open-per-row pattern was paying the ``_migrate_schema`` PRAGMA
    #    cost on every iteration; batching makes a 32-row pass go from
    #    seconds to milliseconds. The transaction is still short
    #    because UPDATEs against an indexed column are fast.
    conn = get_connection()
    try:
        all_rows = conn.execute(
            "SELECT id, file_path FROM session_file_writes"
        ).fetchall()
        updates = [
            (1 if r["file_path"] in dirty else 0, r["id"])
            for r in all_rows
        ]
        dirty_count = sum(u[0] for u in updates)
        conn.executemany(
            "UPDATE session_file_writes SET currently_dirty = ? WHERE id = ?",
            updates,
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "rows_written": rows_written,
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

    # Re-collect porcelain status with codes for the report. Parallel
    # because the refresh already paid the cost once but git state is
    # mutable; running fresh status here gives the report the most
    # up-to-date status code (and a slightly newer set of dirty
    # files, which uncommitted_report tolerates by dropping rows
    # whose path no longer appears).
    from concurrent.futures import ThreadPoolExecutor

    repo_list = list(_discover_repos(repos_root))

    def _scan_with_status(
        repo_path: Path,
    ) -> list[tuple[str, str, str]]:
        raw = _get_status(repo_path)
        out: list[tuple[str, str, str]] = []
        if not raw:
            return out
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
            out.append((abs_path, repo_path.name, status_code))
        return out

    dirty_with_status: dict[str, tuple[str, str]] = {}
    if repo_list:
        with ThreadPoolExecutor(max_workers=min(8, len(repo_list))) as pool:
            for batch in pool.map(_scan_with_status, repo_list):
                for abs_path, repo_name, status_code in batch:
                    dirty_with_status[abs_path] = (repo_name, status_code)

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM session_file_writes "
            "WHERE currently_dirty = 1 "
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

    # Resolve committed_sha lazily, deduped per SHA. Only fire
    # ``git show --name-only`` for SHAs that actually appear in the
    # dirty-row session set, and only once each. A dirty row whose
    # session committed the same file path is dropped from the
    # report — the work isn't actually uncommitted, it just hasn't
    # been pulled or the user has additional later edits.
    committed_files_by_session = _committed_files_for_sessions(
        {r["session_id"] for r in seen_files.values()},
        repos_root,
    )

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
        sid = r["session_id"]
        # Drop if this session also committed the same file.
        if path_obj.as_posix() in committed_files_by_session.get(sid, set()):
            continue
        repo_name, status_code = info
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


def _committed_files_for_sessions(
    session_ids: set[str],
    repos_root: Path,
) -> dict[str, set[str]]:
    """Return ``{session_id: set_of_committed_file_posix_paths}``.

    Deduped per SHA: one ``git show --name-only`` per unique commit
    across all repos, not per (session, file) row. For an
    ``uncommitted_report`` over 32 dirty rows referencing 5 unique
    sessions with ~7 commits each, this is ~35 subprocess calls
    instead of ~4500.

    Empty ``session_ids`` short-circuits to an empty result without
    touching git.
    """
    import subprocess

    from work_buddy.collectors.git_collector import _discover_repos

    if not session_ids:
        return {}

    # Pull the relevant commit SHAs in one DB read.
    placeholders = ",".join(["?"] * len(session_ids))
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT sha, session_id FROM session_commits "
            f"WHERE session_id IN ({placeholders})",
            tuple(session_ids),
        ).fetchall()
    finally:
        conn.close()
    sha_to_sid: dict[str, str] = {r["sha"]: r["session_id"] for r in rows}
    if not sha_to_sid:
        return {sid: set() for sid in session_ids}

    # One git-show per unique SHA per repo. We probe each repo because
    # the SHA might exist in only one of them; failures are silent.
    # Parallelized: with ~200 unique SHAs across 20 repos, serial
    # execution is 30s+; an 8-worker pool drops it to ~4s. Each worker
    # short-circuits on first-found repo.
    from concurrent.futures import ThreadPoolExecutor

    repos = list(_discover_repos(repos_root))

    def _resolve_sha(sha_sid_pair: tuple[str, str]) -> tuple[str, list[str]]:
        sha, sid = sha_sid_pair
        files: list[str] = []
        for repo_path in repos:
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
                if not fname:
                    continue
                try:
                    files.append((repo_path / fname).resolve().as_posix())
                except (ValueError, OSError):
                    continue
            # SHA found in this repo — stop probing other repos.
            break
        return sid, files

    result: dict[str, set[str]] = {sid: set() for sid in session_ids}
    sha_pairs = list(sha_to_sid.items())
    if sha_pairs:
        with ThreadPoolExecutor(max_workers=min(8, len(sha_pairs))) as pool:
            for sid, files in pool.map(_resolve_sha, sha_pairs):
                result[sid].update(files)
    return result


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
