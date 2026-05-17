"""Tests for ProjectMarkdownDB + the project-note format.

Exercises the second concrete MarkdownDB subclass against a temp
projects DB + temp vault: note round-trip, materialization (dry-run +
apply), drift reconciliation (markdown edits → store), orphan handling,
and the apply_mutation dual-surface write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from work_buddy.projects.note_format import (
    ProjectNoteParseError,
    parse_project_note,
    render_project_note,
)


# ════════════════════════════════════════════════════════════════════
# note_format
# ════════════════════════════════════════════════════════════════════


def test_note_roundtrip() -> None:
    text = render_project_note(
        "ecg-fm", "ECG Foundation Model", "active",
        "Multi-line\ndescription body.",
    )
    note = parse_project_note(text)
    assert note.slug == "ecg-fm"
    assert note.name == "ECG Foundation Model"
    assert note.status == "active"
    assert note.description == "Multi-line\ndescription body."


def test_note_missing_frontmatter_raises() -> None:
    with pytest.raises(ProjectNoteParseError, match="frontmatter"):
        parse_project_note("# Just a heading\n\nno frontmatter here")


def test_note_missing_slug_raises() -> None:
    with pytest.raises(ProjectNoteParseError, match="slug"):
        parse_project_note("---\nname: X\nstatus: active\n---\n# X\n")


def test_note_name_falls_back_to_slug() -> None:
    note = parse_project_note("---\nslug: p1\nstatus: active\n---\nbody\n")
    assert note.name == "p1"


def test_note_body_without_h1_kept_verbatim() -> None:
    note = parse_project_note(
        "---\nslug: p1\nname: P1\nstatus: active\n---\n"
        "no heading, straight into prose\n"
    )
    assert note.description == "no heading, straight into prose"


# ════════════════════════════════════════════════════════════════════
# ProjectMarkdownDB — fixtures
# ════════════════════════════════════════════════════════════════════


_MARKDOWN_DIR = "work-buddy/projects"  # vault-relative, matches config default


@pytest.fixture
def projects_env(tmp_path, monkeypatch):
    """Temp vault + temp projects DB with config redirected.

    Project notes live in a single flat directory
    ``<vault>/work-buddy/projects/<slug>.md``. The namespace exposes:
    ``db`` (a fresh ProjectMarkdownDB), ``store``, ``vault``,
    ``notes_dir`` (Path), ``note_path(slug)``, and
    ``write_note(slug, name, status, desc)``.
    """
    vault = tmp_path / "vault"
    notes_dir = vault / "work-buddy" / "projects"
    notes_dir.mkdir(parents=True)
    db_path = tmp_path / "projects.db"

    fake_cfg = {
        "vault_root": str(vault),
        "projects": {
            "db_path": str(db_path),
            "markdown_dir": _MARKDOWN_DIR,
        },
    }

    from work_buddy.projects import store as project_store
    from work_buddy.projects import markdown_db as pmd

    monkeypatch.setattr(project_store, "load_config", lambda *a, **k: fake_cfg)
    monkeypatch.setattr(pmd, "load_config", lambda *a, **k: fake_cfg)

    def note_path(slug: str) -> Path:
        return notes_dir / f"{slug}.md"

    def write_note(slug: str, name: str, status: str, desc: str) -> Path:
        p = note_path(slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            render_project_note(slug, name, status, desc), encoding="utf-8",
        )
        return p

    class Env:
        pass

    env = Env()
    env.vault = vault
    env.notes_dir = notes_dir
    env.store = project_store
    env.db = pmd.ProjectMarkdownDB()
    env.markdown_db_mod = pmd
    env.note_path = note_path
    env.write_note = write_note
    return env


# ════════════════════════════════════════════════════════════════════
# Materialization
# ════════════════════════════════════════════════════════════════════


def test_materialize_dry_run(projects_env) -> None:
    env = projects_env
    env.store.upsert_project("alpha", name="Alpha", description="Desc A")
    env.store.upsert_project("beta", name="Beta", description="Desc B")

    result = env.markdown_db_mod.materialize_projects(dry_run=True)

    assert sorted(result["planned"]) == ["alpha", "beta"]
    assert result["written"] == []
    # No files written.
    assert list(env.notes_dir.glob("*.md")) == []


def test_materialize_apply_writes_notes(projects_env) -> None:
    env = projects_env
    env.store.upsert_project("alpha", name="Alpha", description="Desc A")

    result = env.markdown_db_mod.materialize_projects(dry_run=False)

    assert result["written"] == ["alpha"]
    note = env.note_path("alpha")
    assert note.is_file()
    # The note lives flat in the single directory, not a per-slug subdir.
    assert note.parent == env.notes_dir
    parsed = parse_project_note(note.read_text(encoding="utf-8"))
    assert parsed.name == "Alpha"
    assert parsed.description == "Desc A"


def test_materialize_never_overwrites(projects_env) -> None:
    env = projects_env
    env.store.upsert_project("alpha", name="Alpha", description="STORE desc")
    # A note already exists with different content.
    env.write_note("alpha", "Alpha", "active", "HAND-WRITTEN desc")

    result = env.markdown_db_mod.materialize_projects(dry_run=False)

    assert result["skipped"] == ["alpha"]
    assert result["written"] == []
    # Hand-written content preserved.
    parsed = parse_project_note(env.note_path("alpha").read_text(encoding="utf-8"))
    assert parsed.description == "HAND-WRITTEN desc"


# ════════════════════════════════════════════════════════════════════
# Drift reconciliation
# ════════════════════════════════════════════════════════════════════


def test_reconcile_orphan_note_creates_store_project(projects_env) -> None:
    env = projects_env
    env.write_note("gamma", "Gamma", "active", "Born in the vault")

    report = env.db.reconcile_drift()

    assert "gamma" in report.created
    proj = env.store.get_project("gamma")
    assert proj is not None
    assert proj["name"] == "Gamma"
    assert proj["description"] == "Born in the vault"


def test_reconcile_description_edit_propagates_to_store(projects_env) -> None:
    env = projects_env
    env.store.upsert_project("delta", name="Delta", description="old desc")
    # Materialize, then simulate a hand-edit in Obsidian.
    env.markdown_db_mod.materialize_projects(dry_run=False)
    env.write_note("delta", "Delta", "active", "EDITED IN OBSIDIAN")

    report = env.db.reconcile_drift()

    assert any(d["pk"] == "delta" for d in report.drift["description"])
    assert env.store.get_project("delta")["description"] == "EDITED IN OBSIDIAN"


def test_reconcile_status_edit_propagates(projects_env) -> None:
    env = projects_env
    env.store.upsert_project("eps", name="Eps", status="active",
                             description="d")
    env.write_note("eps", "Eps", "paused", "d")

    env.db.reconcile_drift()

    assert env.store.get_project("eps")["status"] == "paused"


def test_reconcile_does_not_delete_orphan_in_store_pre_materialization(
    projects_env,
) -> None:
    """ProjectMarkdownDB.delete_orphans_in_store is False — a store
    project with no note must be left intact, not soft-deleted. This
    is the guard against the first reconcile pass (before any note
    exists) wiping the whole registry."""
    env = projects_env
    env.store.upsert_project("ghost", name="Ghost", description="only in DB")
    # No note on disk for 'ghost'.

    report = env.db.reconcile_drift()

    assert report.deleted == []
    # Still present — not soft-deleted.
    assert env.store.get_project("ghost") is not None


def test_reconcile_deletes_orphan_when_flag_enabled(projects_env) -> None:
    """With delete_orphans_in_store flipped True (the post-cutover
    state), an orphan-in-store IS soft-deleted."""
    env = projects_env
    env.store.upsert_project("ghost", name="Ghost", description="only in DB")
    env.db.delete_orphans_in_store = True  # simulate post-cutover

    report = env.db.reconcile_drift()

    assert "ghost" in report.deleted
    assert env.store.get_project("ghost") is None


def _make_malformed_note(env, slug: str) -> None:
    env.store.upsert_project(slug, name=slug.title(), description="d")
    env.note_path(slug).write_text(
        "# " + slug.title() + "\n\nno frontmatter, not a project note\n",
        encoding="utf-8",
    )


def test_reconcile_keeps_project_with_unparseable_note(projects_env) -> None:
    """A store project whose note file exists but fails to parse must
    NOT be soft-deleted, even with delete_orphans_in_store=True — a
    malformed note is a fixable error, not a deletion signal."""
    env = projects_env
    _make_malformed_note(env, "malformed")
    env.db.delete_orphans_in_store = True  # even with deletion enabled

    report = env.db.reconcile_drift()

    assert "malformed" not in report.deleted
    assert env.store.get_project("malformed") is not None
    assert any("malformed" in w for w in report.warnings)


def test_reconcile_warns_on_unparseable_note_when_delete_disabled(
    projects_env,
) -> None:
    """The malformed-note warning fires regardless of
    delete_orphans_in_store — a non-conforming note is an
    attention-needed condition independent of the delete policy."""
    env = projects_env
    _make_malformed_note(env, "malformed")
    assert env.db.delete_orphans_in_store is False  # ProjectMarkdownDB default

    report = env.db.reconcile_drift()

    assert "malformed" not in report.deleted
    assert any("malformed" in w for w in report.warnings)


def test_materialize_blocks_on_non_conforming_file(projects_env) -> None:
    """A project whose note path holds a non-conforming file is reported
    'blocked' (not 'skipped', not overwritten)."""
    env = projects_env
    env.store.upsert_project("blockme", name="Block Me", description="d")
    note = env.note_path("blockme")
    note.write_text("not a project note\n", encoding="utf-8")

    result = env.markdown_db_mod.materialize_projects(dry_run=False)

    assert result["blocked"] == ["blockme"]
    assert "blockme" not in result["written"]
    assert "blockme" not in result["skipped"]
    # The non-conforming file is left exactly as it was.
    assert note.read_text(encoding="utf-8") == "not a project note\n"


def test_parse_skips_slug_filename_mismatch(projects_env) -> None:
    """A note whose frontmatter slug disagrees with its filename is
    skipped — the filename is authoritative for the single-dir layout."""
    env = projects_env
    # File named wrong-name.md but frontmatter claims slug 'realproj'.
    (env.notes_dir / "wrong-name.md").write_text(
        render_project_note("realproj", "Real Project", "active", "desc"),
        encoding="utf-8",
    )
    # A correctly-named note alongside it.
    env.write_note("goodproj", "Good Project", "active", "desc")

    parsed = env.db.parse_all_from_markdown()

    assert "goodproj" in parsed
    assert "realproj" not in parsed  # filename/slug mismatch → skipped
    assert "wrong-name" not in parsed


def test_reconcile_in_sync_is_noop(projects_env) -> None:
    env = projects_env
    env.store.upsert_project("zeta", name="Zeta", description="stable")
    env.markdown_db_mod.materialize_projects(dry_run=False)

    report = env.db.reconcile_drift()
    assert not report.changed


def test_apply_mutation_writes_both_surfaces(projects_env) -> None:
    env = projects_env
    env.store.upsert_project("eta", name="Eta", description="initial")
    env.markdown_db_mod.materialize_projects(dry_run=False)

    from work_buddy.markdown_db import WriteProvenance
    env.db.apply_mutation(
        "eta", {"description": "via dashboard"},
        provenance=WriteProvenance.mutation(frozenset({"user"}), "dashboard"),
    )

    # Store surface.
    assert env.store.get_project("eta")["description"] == "via dashboard"
    # Markdown surface.
    parsed = parse_project_note(env.note_path("eta").read_text(encoding="utf-8"))
    assert parsed.description == "via dashboard"


def test_apply_mutation_records_user_author(projects_env) -> None:
    """A WriteProvenance with actor={user} (a dashboard edit) is recorded
    as author='user' in the project's revision history — not 'agent'."""
    env = projects_env
    env.store.upsert_project("theta", name="Theta", description="initial")
    env.markdown_db_mod.materialize_projects(dry_run=False)

    from work_buddy.markdown_db import WriteProvenance
    env.db.apply_mutation(
        "theta", {"description": "user edit"},
        provenance=WriteProvenance.mutation(frozenset({"user"}), "dashboard"),
    )

    pid = env.store.resolve_slug("theta")
    latest = env.store.list_revisions(pid, limit=1)[0]
    assert latest["author"] == "user"


def test_drift_reconcile_records_agent_author(projects_env) -> None:
    """A drift-reconciliation store write is recorded as author='agent'
    — the reconciler acted, the originating human was not observed."""
    env = projects_env
    env.store.upsert_project("iota", name="Iota", description="old")
    env.markdown_db_mod.materialize_projects(dry_run=False)
    # Out-of-band edit to the note, then reconcile propagates it.
    env.write_note("iota", "Iota", "active", "edited out of band")
    env.db.reconcile_drift()

    pid = env.store.resolve_slug("iota")
    latest = env.store.list_revisions(pid, limit=1)[0]
    assert latest["author"] == "agent"
