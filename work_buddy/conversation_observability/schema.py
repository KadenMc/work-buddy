"""SQL schema for the conversation-observability store.

Four tables:

* ``observed_sessions`` — one row per Claude Code JSONL session we've
  scanned. Tracks source path + mtime + observation metadata so a
  stale-only refresh can skip unchanged sessions.
* ``session_commits`` — one row per git commit attributed to a session.
* ``session_file_writes`` — one row per (session, file_path) write
  event. ``currently_dirty`` is a snapshot of the most recent
  observation; treat it as best-effort, not authoritative.
* ``session_prs`` — one row per (session, PR, action) GitHub pull-request
  event, attributed by detecting ``gh pr create|merge|close|review``
  Bash invocations in the session JSONL (structural detection, not
  commit-message parsing).

Foreign keys cascade-on-parent-delete via the SqliteRowsStorage
``post_delete_sql`` hook, not via SQLite's own FK enforcement (which is
off by default and adds gotchas around connection pragmas). The
artifact registration is the single source of truth for cleanup.

Summary data (``session_summaries`` + ``topic_summaries``) was migrated
into ``summarization.db`` on 2026-05-28 and the legacy tables dropped.
The summarization framework's ``summary_items`` + ``summary_nodes``
are now the canonical source; dashboard / collectors / adapter read
from there.
"""

from __future__ import annotations

SCHEMA = """\
-- ``source_mtime`` records the file mtime at the last *metadata* load
-- (``refresh_observed_sessions``). Separate per-concern columns track
-- when commits / writes were last extracted, so the three refreshers
-- don't clobber each other's staleness state.
CREATE TABLE IF NOT EXISTS observed_sessions (
    session_id              TEXT PRIMARY KEY,
    project_name            TEXT,
    project_slug            TEXT,
    source_path             TEXT NOT NULL,
    source_mtime            REAL NOT NULL,
    observed_at             TEXT NOT NULL,
    start_time              TEXT,
    end_time                TEXT,
    message_count           INTEGER,
    span_count              INTEGER,
    tool_names_json         TEXT NOT NULL DEFAULT '{}',
    status                  TEXT NOT NULL DEFAULT 'ok',
    error                   TEXT,
    commits_scanned_mtime   REAL,
    writes_scanned_mtime    REAL
);

CREATE INDEX IF NOT EXISTS idx_observed_sessions_project
    ON observed_sessions(project_name);
CREATE INDEX IF NOT EXISTS idx_observed_sessions_observed_at
    ON observed_sessions(observed_at);

CREATE TABLE IF NOT EXISTS session_commits (
    sha             TEXT PRIMARY KEY,
    short_sha       TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    repo_path       TEXT,
    repo_name       TEXT,
    branch          TEXT,
    message         TEXT,
    files_changed   INTEGER,
    committed_at    TEXT,
    message_index   INTEGER,
    observed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_commits_session
    ON session_commits(session_id);
CREATE INDEX IF NOT EXISTS idx_session_commits_short_sha
    ON session_commits(short_sha);
CREATE INDEX IF NOT EXISTS idx_session_commits_committed_at
    ON session_commits(committed_at);

-- One row per (session, PR, action) pull-request event. ``action`` is a
-- column rather than a table-per-verb because the four verbs share the
-- same shape (session, PR, time, URL) and the dashboard wants a flat
-- activity stream. The UNIQUE key includes ``ts`` because the same
-- session can review the same PR more than once; create/merge timestamps
-- are stable so re-ingestion is idempotent via ``INSERT OR IGNORE``.
CREATE TABLE IF NOT EXISTS session_prs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    pr_url          TEXT NOT NULL,
    repo            TEXT NOT NULL,
    action          TEXT NOT NULL,
    ts              TEXT,
    message_index   INTEGER,
    source_path     TEXT,
    observed_at     TEXT NOT NULL,
    UNIQUE(session_id, pr_number, action, ts)
);

CREATE INDEX IF NOT EXISTS idx_session_prs_session
    ON session_prs(session_id);
CREATE INDEX IF NOT EXISTS idx_session_prs_pr
    ON session_prs(pr_number);

CREATE TABLE IF NOT EXISTS session_file_writes (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    write_timestamp TEXT,
    message_index   INTEGER,
    committed_sha   TEXT,
    currently_dirty INTEGER NOT NULL DEFAULT 0,
    observed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_file_writes_session
    ON session_file_writes(session_id);
CREATE INDEX IF NOT EXISTS idx_session_file_writes_dirty
    ON session_file_writes(currently_dirty);
"""

# `session_summaries` + `topic_summaries` were dropped on 2026-05-28 after
# the framework migration. Their data was one-shot-migrated into
# `summarization.db`'s `summary_items` + `summary_nodes` tables. Don't
# resurrect the schema here — the dashboard + adapter both read from the
# framework now.
