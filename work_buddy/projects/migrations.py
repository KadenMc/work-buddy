"""Versioned schema migrations for the projects SQLite store.

Adopts the shared ``MigrationRunner`` framework (see
``work_buddy/storage/migrations.py``). The ladder lifts a pre-framework
DB through the relational temporal redesign:

  v1 → legacy schema (matches pre-framework existing DBs)
  v2 → surrogate ``id`` PK, ``origin`` column, status enum tightening
  v3 → ``project_folders`` and ``project_aliases`` child tables
  v4 → ``project_revisions`` + history tables for folders and aliases
  v5 → data migration (slug renames, folder/alias backfill,
       initial revision rows)
  v6 → fold ecg-cred into ecg-inquiry
  v7 → lww_meta append-only write-provenance sidecar (MarkdownDB)

Migration 1 reproduces the legacy schema verbatim so that existing
DBs at ``user_version=0`` baseline-stamp cleanly at v1, then run
v2-v5 forward. Fresh installs build v1 from scratch and lift to v5.

Migrations are idempotent. A failing migration rolls back its full
step under the framework's per-step transaction.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.storage.migrations import Migration, MigrationRunner

logger = get_logger(__name__)


# ─── Helpers ────────────────────────────────────────────────────────


def _now_ms() -> str:
    """Millisecond-precision ISO 8601 UTC timestamp.

    Matches the format the projects store writes going forward and
    satisfies the ``GLOB '????-??-??T??:??:??*'`` CHECK constraint.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_slug(name: str) -> str:
    """Project slug normalization: lowercase, ``_`` and space → ``-``."""
    return name.lower().replace("_", "-").replace(" ", "-")


# ─── Migration 1 — legacy schema baseline ───────────────────────────


def _m001_legacy_baseline(conn: sqlite3.Connection) -> None:
    """Legacy schema. Matches what pre-framework DBs already have.

    Existing DBs baseline-stamp here (the inferrer below returns 1
    when it sees the legacy shape). Fresh installs run this as
    the genesis of the ladder.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            slug         TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'active',
            description  TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_projects_status
            ON projects(status);
        """
    )


# ─── Migration 2 — surrogate id, origin, tightened enum ─────────────


def _m002_surrogate_id_and_origin(conn: sqlite3.Connection) -> None:
    """Add ``id INTEGER PRIMARY KEY`` surrogate, ``origin`` column,
    and CHECK constraints. Flips any ``status='inferred'`` rows to
    ``'active'`` so the new CHECK doesn't reject them.

    SQLite can't ALTER a primary key in place — we use the standard
    rebuild dance (create new table, INSERT … SELECT, drop old,
    rename new). The runner has already disabled foreign keys for
    this step.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "id" in cols and "origin" in cols:
        return  # already at v2 shape

    # Pre-flip 'inferred' status so the new CHECK accepts everything.
    conn.execute("UPDATE projects SET status='active' WHERE status='inferred'")

    # Detect existing vault directories per slug → origin backfill.
    origin_map = _build_origin_map(conn)

    conn.executescript(
        """
        CREATE TABLE projects_new (
            id           INTEGER PRIMARY KEY,
            slug         TEXT NOT NULL UNIQUE,
            name         TEXT NOT NULL,
            status       TEXT NOT NULL CHECK(status IN
                ('active','paused','past','future','deleted')),
            description  TEXT,
            origin       TEXT NOT NULL CHECK(origin IN ('vault','manual')),
            created_at   TEXT NOT NULL CHECK(created_at GLOB
                '????-??-??T??:??:??*'),
            updated_at   TEXT NOT NULL CHECK(updated_at GLOB
                '????-??-??T??:??:??*')
        );
        """
    )

    rows = conn.execute(
        "SELECT slug, name, status, description, created_at, updated_at "
        "FROM projects"
    ).fetchall()
    for slug, name, status, description, created_at, updated_at in rows:
        origin = origin_map.get(slug, "manual")
        conn.execute(
            "INSERT INTO projects_new "
            "(slug, name, status, description, origin, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, name, status, description, origin, created_at, updated_at),
        )

    conn.execute("DROP INDEX IF EXISTS idx_projects_status")
    conn.execute("DROP TABLE projects")
    conn.execute("ALTER TABLE projects_new RENAME TO projects")
    conn.execute("CREATE INDEX idx_projects_status ON projects(status)")


def _build_origin_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{slug: 'vault' | 'manual'}`` for every row in projects.

    A row's origin is ``'vault'`` if a directory exists under
    ``<vault_root>/work/projects/<slug>/`` or one of the lifecycle
    subdirectories (``projects-past/<slug>/``, ``projects-future/<slug>/``)
    whose normalized name matches the slug. Otherwise ``'manual'``.

    Config is loaded lazily so the test path doesn't pay it.
    """
    try:
        from work_buddy.config import load_config
        cfg = load_config()
        vault_root_raw = cfg.get("vault_root")
    except Exception:
        vault_root_raw = None

    out: dict[str, str] = {}
    if not vault_root_raw:
        # No vault root configured — everything is manual.
        for (slug,) in conn.execute("SELECT slug FROM projects"):
            out[slug] = "manual"
        return out

    vault_root = Path(vault_root_raw)
    work_projects = vault_root / "work" / "projects"
    bases: list[Path] = [
        work_projects,
        work_projects / "projects-past",
        work_projects / "projects-future",
    ]

    # Build a slug → has_dir lookup by scanning the bases once.
    vault_slugs: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        try:
            for entry in base.iterdir():
                if entry.is_dir():
                    vault_slugs.add(_normalize_slug(entry.name))
        except OSError:
            continue

    for (slug,) in conn.execute("SELECT slug FROM projects"):
        out[slug] = "vault" if slug in vault_slugs else "manual"
    return out


# ─── Migration 3 — folder and alias child tables ────────────────────


def _m003_folders_and_aliases(conn: sqlite3.Connection) -> None:
    """Create the per-project folders and aliases tables."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_folders (
            id            INTEGER PRIMARY KEY,
            project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
            path          TEXT NOT NULL,
            archived      INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
            UNIQUE(project_id, path)
        );

        CREATE TABLE IF NOT EXISTS project_aliases (
            id            INTEGER PRIMARY KEY,
            project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
            alias         TEXT NOT NULL,
            alias_norm    TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_project_aliases_project_id
            ON project_aliases(project_id);
        """
    )


# ─── Migration 4 — revision history tables ──────────────────────────


def _m004_revisions_and_history(conn: sqlite3.Connection) -> None:
    """Create the append-only revision history tables.

    ``project_revisions`` carries the projects-row snapshot plus audit
    fields. ``project_folders_history`` and ``project_aliases_history``
    capture the variable-length child rows at each revision. None of
    the history tables hold an FK back to ``projects(id)`` for the
    revision's ``project_id`` — that link must survive deletion
    semantics and slug renames cleanly.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_revisions (
            id                  INTEGER PRIMARY KEY,
            project_id          INTEGER NOT NULL,
            project_slug        TEXT NOT NULL,
            name                TEXT NOT NULL,
            status              TEXT NOT NULL,
            description         TEXT,
            origin              TEXT NOT NULL,
            author              TEXT NOT NULL CHECK(author IN ('user','agent')),
            created_at          TEXT NOT NULL CHECK(created_at GLOB
                '????-??-??T??:??:??*'),
            user_confirmed_at   TEXT,
            change_summary      TEXT,
            schema_version      INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_pr_project_id_created
            ON project_revisions(project_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pr_created_at
            ON project_revisions(created_at);

        CREATE TABLE IF NOT EXISTS project_folders_history (
            id            INTEGER PRIMARY KEY,
            revision_id   INTEGER NOT NULL REFERENCES project_revisions(id),
            path          TEXT NOT NULL,
            archived      INTEGER NOT NULL CHECK(archived IN (0,1))
        );
        CREATE INDEX IF NOT EXISTS idx_pfh_revision_id
            ON project_folders_history(revision_id);
        CREATE INDEX IF NOT EXISTS idx_pfh_path
            ON project_folders_history(path);

        CREATE TABLE IF NOT EXISTS project_aliases_history (
            id            INTEGER PRIMARY KEY,
            revision_id   INTEGER NOT NULL REFERENCES project_revisions(id),
            alias         TEXT NOT NULL,
            alias_norm    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pah_revision_id
            ON project_aliases_history(revision_id);
        """
    )


# ─── Migration 5 — data migration ───────────────────────────────────


# Slug rename map: (old_slug, new_slug, [(alias_display, alias_norm), ...])
_RENAMES: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("aexp", "agentic-experiments", [("aexp", "aexp")]),
    ("uhn-ecg-deploy", "uhn-ecg-ai-deploy", []),
    (
        "electricrag",
        "ecg-inquiry",
        [("ElectricRAG", "electricrag"), ("ECG-CRED", "ecg-cred")],
    ),
]

# Per-slug status overrides applied during the data migration.
_STATUS_OVERRIDES: dict[str, str] = {
    "ecg-fm": "past",  # Published JAMIA Open 2025
}


def _m005_data_migration(conn: sqlite3.Connection) -> None:
    """Slug renames, folder + alias backfill, write initial revisions.

    Idempotent: only renames a slug if the old one still exists and
    the new one doesn't. Only writes initial revisions for projects
    that have no revision row yet.
    """
    # Apply slug renames + populate aliases for the rename targets.
    for old_slug, new_slug, alias_pairs in _RENAMES:
        old_row = conn.execute(
            "SELECT id FROM projects WHERE slug=?", (old_slug,)
        ).fetchone()
        new_row = conn.execute(
            "SELECT id FROM projects WHERE slug=?", (new_slug,)
        ).fetchone()
        if old_row and new_row:
            raise RuntimeError(
                f"Migration cannot rename {old_slug!r} -> {new_slug!r}: "
                f"both slugs already exist. Manual intervention needed."
            )
        if old_row:
            conn.execute(
                "UPDATE projects SET slug=? WHERE slug=?", (new_slug, old_slug)
            )
            project_id = old_row[0]
            for alias_display, alias_norm in alias_pairs:
                conn.execute(
                    "INSERT OR IGNORE INTO project_aliases "
                    "(project_id, alias, alias_norm) VALUES (?, ?, ?)",
                    (project_id, alias_display, alias_norm),
                )
            logger.info(
                "Renamed project %r -> %r (aliases: %d)",
                old_slug, new_slug, len(alias_pairs),
            )

    # Apply status overrides per the PhD-doc context.
    for slug, new_status in _STATUS_OVERRIDES.items():
        conn.execute(
            "UPDATE projects SET status=? WHERE slug=?", (new_status, slug)
        )

    # Backfill folders for every slug that has known paths.
    folders_map = _build_folders_map()
    for slug, folder_list in folders_map.items():
        row = conn.execute(
            "SELECT id FROM projects WHERE slug=?", (slug,)
        ).fetchone()
        if not row:
            continue
        project_id = row[0]
        for path_str, archived in folder_list:
            try:
                exists = Path(path_str).exists()
            except OSError:
                exists = False
            if not exists:
                logger.warning(
                    "Project %r: backfilled folder does not exist on disk: %s",
                    slug, path_str,
                )
            conn.execute(
                "INSERT OR IGNORE INTO project_folders "
                "(project_id, path, archived) VALUES (?, ?, ?)",
                (project_id, path_str, archived),
            )

    # Write initial revisions for every project that doesn't have one yet.
    now = _now_ms()
    rows = conn.execute(
        "SELECT id, slug, name, status, description, origin FROM projects "
        "ORDER BY slug"
    ).fetchall()
    for project_id, slug, name, status, description, origin in rows:
        already = conn.execute(
            "SELECT 1 FROM project_revisions WHERE project_id=? LIMIT 1",
            (project_id,),
        ).fetchone()
        if already:
            continue
        conn.execute(
            "INSERT INTO project_revisions ("
            "  project_id, project_slug, name, status, description, origin,"
            "  author, created_at, user_confirmed_at, change_summary, "
            "  schema_version"
            ") VALUES (?, ?, ?, ?, ?, ?, 'agent', ?, NULL, ?, 1)",
            (
                project_id, slug, name, status, description, origin, now,
                "initial migration from pre-temporal schema",
            ),
        )
        revision_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Snapshot folders and aliases as they stand right after backfill.
        for path, archived in conn.execute(
            "SELECT path, archived FROM project_folders WHERE project_id=? "
            "ORDER BY path",
            (project_id,),
        ):
            conn.execute(
                "INSERT INTO project_folders_history "
                "(revision_id, path, archived) VALUES (?, ?, ?)",
                (revision_id, path, archived),
            )
        for alias, alias_norm in conn.execute(
            "SELECT alias, alias_norm FROM project_aliases WHERE project_id=? "
            "ORDER BY alias_norm",
            (project_id,),
        ):
            conn.execute(
                "INSERT INTO project_aliases_history "
                "(revision_id, alias, alias_norm) VALUES (?, ?, ?)",
                (revision_id, alias, alias_norm),
            )


def _build_folders_map() -> dict[str, list[tuple[str, int]]]:
    """Per-slug folder backfill list. Paths are absolute system paths
    resolved against the current ``vault_root`` config.

    Folders flagged ``archived=1`` are kept on the project but marked
    dormant (e.g. unique-IP repos that aren't currently developed).
    """
    try:
        from work_buddy.config import load_config
        cfg = load_config()
        vault_root_raw = cfg.get("vault_root")
    except Exception:
        vault_root_raw = None

    if not vault_root_raw:
        return {}

    vault_root = Path(vault_root_raw)

    def vp(*parts: str) -> str:
        return str(vault_root.joinpath(*parts))

    return {
        "ecg-fm": [
            (vp("work", "projects", "ecg-fm"), 0),
            (vp("repos", "foundational-ecg"), 0),
        ],
        "ecg-agent": [
            (vp("work", "projects", "ecg-agent"), 0),
            (vp("repos", "ecg-agent-dev"), 0),
        ],
        "ecg-cred": [
            (vp("work", "projects", "ecg-cred"), 0),
        ],
        "ecg-inquiry": [
            (vp("repos", "electricrag"), 0),
            (vp("repos", "ecg-cred-dev"), 1),
            (vp("repos", "electricrag-extra"), 1),
        ],
        "agentic-experiments": [
            (vp("repos", "agentic-experiments"), 0),
        ],
        "work-buddy": [
            (vp("repos", "work-buddy"), 0),
        ],
        "sdr": [
            (vp("repos", "sdr"), 0),
        ],
        "uhn-ecg-ai-deploy": [
            (vp("repos", "uhn-ecg-ai-deploy"), 0),
        ],
        "contrastive-fmri": [
            (vp("work", "projects", "projects-past", "Contrastive fMRI"), 0),
        ],
        "diffusiveharmony": [
            (vp("work", "projects", "projects-past", "DiffusiveHarmony"), 0),
        ],
        "smartbrain": [
            (vp("work", "projects", "projects-past", "SMaRTBRAIN"), 0),
        ],
        "crania-eeg": [
            (vp("work", "admin", "Institutions + Groups", "CRANIA"), 0),
        ],
    }


# ─── Migration 6 — fold ecg-cred into ecg-inquiry ───────────────────


def _m006_fold_ecg_cred_into_ecg_inquiry(conn: sqlite3.Connection) -> None:
    """Soft-delete the standalone ``ecg-cred`` row and absorb its vault
    folder into ``ecg-inquiry``.

    Rationale: ``ECG-CRED`` is a prior name for the ECG-Inquiry research
    project (per the PhD doc), not a separate project. The v5 migration
    correctly attached ``ECG-CRED`` as an alias of ``ecg-inquiry``, but
    ``ecg-cred`` also existed as a vault-canonical row (its
    ``work/projects/ecg-cred/`` directory drives auto-creation). With
    both present, ``resolve_slug("ECG-CRED")`` would short-circuit on
    the live canonical row and never reach the alias.

    Fix:
    1. Soft-delete ``ecg-cred`` (preserves history).
    2. Add its ``work/projects/ecg-cred/`` path as a folder on
       ``ecg-inquiry`` (so the user's existing notes still attach to
       the right project on next sync).
    3. Record both changes as revision rows on the affected projects.

    Idempotent: skips if ``ecg-cred`` is already soft-deleted or the
    folder is already attached to ``ecg-inquiry``.
    """
    now = _now_ms()

    # Locate both rows.
    src = conn.execute(
        "SELECT id, status FROM projects WHERE slug='ecg-cred'"
    ).fetchone()
    dst = conn.execute(
        "SELECT id FROM projects WHERE slug='ecg-inquiry'"
    ).fetchone()

    if not src or not dst:
        # Either side missing — nothing to fold.
        return

    src_id, src_status = src[0], src[1]
    dst_id = dst[0]

    if src_status == "deleted":
        return  # already done

    # Pull ecg-cred's folder list to copy across.
    src_folders = conn.execute(
        "SELECT path, archived FROM project_folders WHERE project_id=?",
        (src_id,),
    ).fetchall()

    # Soft-delete the source row.
    conn.execute(
        "UPDATE projects SET status='deleted', updated_at=? WHERE id=?",
        (now, src_id),
    )

    # Copy each source folder to the destination if not already present.
    # Mark the source folder as archived=1 on the destination — it's
    # historical notes, not active workspace.
    for path, archived in src_folders:
        existing = conn.execute(
            "SELECT 1 FROM project_folders WHERE project_id=? AND path=?",
            (dst_id, path),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO project_folders (project_id, path, archived) "
                "VALUES (?, ?, 1)",
                (dst_id, path),
            )

    # Write a revision on the source recording the soft-delete.
    conn.execute(
        "INSERT INTO project_revisions ("
        "  project_id, project_slug, name, status, description, origin,"
        "  author, created_at, user_confirmed_at, change_summary, "
        "  schema_version"
        ") SELECT id, slug, name, status, description, origin,"
        "  'agent', ?, NULL, ?, 1 FROM projects WHERE id=?",
        (now, "folded into ecg-inquiry (ECG-CRED is a prior name)", src_id),
    )

    # Snapshot empty folders/aliases on the source revision (it had
    # folders, but the soft-delete revision captures the new state;
    # post-soft-delete the source no longer "owns" them conceptually).
    # We still record what was there at the moment of the soft-delete:
    src_rev = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for path, archived in src_folders:
        conn.execute(
            "INSERT INTO project_folders_history "
            "(revision_id, path, archived) VALUES (?, ?, ?)",
            (src_rev, path, archived),
        )

    # Write a revision on the destination recording the absorption.
    conn.execute(
        "INSERT INTO project_revisions ("
        "  project_id, project_slug, name, status, description, origin,"
        "  author, created_at, user_confirmed_at, change_summary, "
        "  schema_version"
        ") SELECT id, slug, name, status, description, origin,"
        "  'agent', ?, NULL, ?, 1 FROM projects WHERE id=?",
        (now, "absorbed ecg-cred folders (ECG-CRED prior-name merger)", dst_id),
    )
    dst_rev = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for path, archived in conn.execute(
        "SELECT path, archived FROM project_folders WHERE project_id=? "
        "ORDER BY path",
        (dst_id,),
    ):
        conn.execute(
            "INSERT INTO project_folders_history "
            "(revision_id, path, archived) VALUES (?, ?, ?)",
            (dst_rev, path, archived),
        )
    for alias, alias_norm in conn.execute(
        "SELECT alias, alias_norm FROM project_aliases WHERE project_id=? "
        "ORDER BY alias_norm",
        (dst_id,),
    ):
        conn.execute(
            "INSERT INTO project_aliases_history "
            "(revision_id, alias, alias_norm) VALUES (?, ?, ?)",
            (dst_rev, alias, alias_norm),
        )

    logger.info(
        "Folded ecg-cred (id=%d) into ecg-inquiry (id=%d): "
        "soft-deleted source, copied %d folder(s) as archived",
        src_id, dst_id, len(src_folders),
    )


# ─── Custom runner with shape-aware baseline inference ──────────────


class _ProjectsMigrationRunner(MigrationRunner):
    """Projects-specific runner. Overrides baseline inference so a
    pre-framework DB at the legacy shape is stamped at v1 (not at
    ``target_version`` like the default heuristic).
    """

    def _infer_baseline_version(self, conn: sqlite3.Connection) -> int:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' "
                "AND name != '_migration_history'"
            )
        }
        if not tables:
            return 0
        if "projects" not in tables:
            return 0
        cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
        if "id" not in cols:
            # Pre-framework legacy shape: slug-as-PK, no origin column.
            return 1
        # Already at (or past) v2 — assume fully migrated.
        return self.target_version


# ─── Runner instance ────────────────────────────────────────────────


# ─── Migration 7 — lww_meta sidecar (MarkdownDB write-provenance) ────


def _m007_lww_meta(conn: sqlite3.Connection) -> None:
    """Create the ``lww_meta`` append-only write-provenance table.

    Backs :class:`work_buddy.markdown_db.SqliteLwwLog` for the
    markdown-canonical projects surface (see ``architecture/markdown-db``).
    Co-located in ``projects.db`` so it travels with backups + restores.
    Every write through a :class:`~work_buddy.markdown_db.MarkdownDB`
    appends one row per field per surface; nothing is updated or deleted.

    The DDL is inlined (not imported from ``markdown_db.sqlite_lww``)
    because the migration runner hashes this callable's source — the
    installed schema must be visible here for the audit to catch a
    change. Keep byte-identical to ``markdown_db.sqlite_lww.LWW_META_DDL``.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lww_meta (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name    TEXT NOT NULL,
            row_pk        TEXT NOT NULL,
            field         TEXT NOT NULL,
            ts            TEXT NOT NULL,
            actor         TEXT NOT NULL DEFAULT '[]',
            process       TEXT NOT NULL,
            from_surface  TEXT,
            to_surface    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_lww_meta_latest
            ON lww_meta(table_name, row_pk, field, to_surface, ts);
    """)


# ─── Runner instance ────────────────────────────────────────────────


PROJECT_MIGRATIONS = _ProjectsMigrationRunner(
    "projects",
    migrations=[
        Migration(1, "legacy baseline schema",
                  _m001_legacy_baseline),
        Migration(2, "surrogate id + origin + tightened status enum",
                  _m002_surrogate_id_and_origin),
        Migration(3, "project_folders + project_aliases tables",
                  _m003_folders_and_aliases),
        Migration(4, "revision history tables",
                  _m004_revisions_and_history),
        Migration(5, "data migration (renames, backfill, initial revisions)",
                  _m005_data_migration),
        Migration(6, "fold ecg-cred into ecg-inquiry",
                  _m006_fold_ecg_cred_into_ecg_inquiry),
        Migration(7, "lww_meta write-provenance sidecar",
                  _m007_lww_meta),
    ],
)
