"""assess_state + pii_check back the dev-commit workflow's auto_run steps.

These are deterministic offloaders: we pin their shapes and the
specific patterns that must fire, because agents running dev-commit
rely on the output structure to drive branch-guard, test, cleanup,
and record decisions.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.dev import commit as dev_commit


@pytest.fixture
def fake_git():
    state = {"branch": "feat/x", "tracked": [], "untracked": []}

    def _fake(*args: str) -> list[str]:
        if args[:2] == ("branch", "--show-current"):
            return [state["branch"]] if state["branch"] else []
        if args[:1] == ("diff",):
            return state["tracked"]
        if args[:1] == ("ls-files",):
            return state["untracked"]
        return []

    # dev.commit imports _run_git from dev.document at module load, so we
    # must patch the binding where it's used, not where it's defined.
    with patch("work_buddy.dev.commit._run_git", side_effect=_fake):
        yield state


def test_assess_state_clean_branch(fake_git):
    fake_git["branch"] = "feat/new-thing"
    fake_git["tracked"] = ["work_buddy/dev/commit.py"]
    fake_git["untracked"] = []

    result = dev_commit.assess_state()
    assert result["current_branch"] == "feat/new-thing"
    assert result["is_main"] is False
    assert "work_buddy/dev/commit.py" in result["classified"]["module"]
    # No on-main warning
    assert not any("protected branch" in w for w in result["warnings"])


def test_assess_state_on_main_warns(fake_git):
    fake_git["branch"] = "main"
    fake_git["tracked"] = ["work_buddy/x.py"]
    fake_git["untracked"] = []

    result = dev_commit.assess_state()
    assert result["is_main"] is True
    assert any("protected branch" in w.lower() for w in result["warnings"])


def test_assess_state_knowledge_edits_warn(fake_git):
    fake_git["branch"] = "feat/x"
    fake_git["tracked"] = ["knowledge/store/tasks.md"]
    fake_git["untracked"] = []

    result = dev_commit.assess_state()
    assert any(
        "docs_edit" in w or "reconcil" in w.lower() for w in result["warnings"]
    ), result["warnings"]


def test_assess_state_guesses_test_candidates(fake_git, tmp_path, monkeypatch):
    """Module edits should surface matching test files if they exist."""
    repo = tmp_path
    (repo / "work_buddy" / "dev").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_commit.py").write_text("", encoding="utf-8")

    monkeypatch.setattr("work_buddy.dev.commit.repo_root", lambda: repo)

    fake_git["branch"] = "feat/x"
    fake_git["tracked"] = ["work_buddy/dev/commit.py"]
    fake_git["untracked"] = []

    result = dev_commit.assess_state()
    assert "tests/unit/test_commit.py" in result["test_candidates"]


def test_pii_check_flags_personal_paths(tmp_path, monkeypatch):
    """The core guardrail: personal paths must be caught.

    Test fixtures assembled piecewise so this file itself stays clean
    when the detector runs against the whole repo.
    """
    monkeypatch.setattr("work_buddy.dev.commit.repo_root", lambda: tmp_path)
    bad_win = "C:/" + "Vaults/" + "Second" + "Brain" + "/repos/x"
    bad_posix = "/Users/" + "alice/data"
    f = tmp_path / "example.py"
    f.write_text(
        f'PATH = "{bad_win}"\n'
        f'OTHER = "{bad_posix}"\n'
        'CLEAN = "work_buddy/dev/commit.py"\n',
        encoding="utf-8",
    )

    result = dev_commit.pii_check(files=["example.py"])
    assert result["clean"] is False
    labels = [h["label"] for h in result["hits"]]
    assert "windows-vault-path" in labels
    assert "personal-vault-name" in labels or "windows-vault-path" in labels
    assert "posix-user-home" in labels


def test_pii_check_clean(tmp_path, monkeypatch):
    monkeypatch.setattr("work_buddy.dev.commit.repo_root", lambda: tmp_path)
    f = tmp_path / "ok.py"
    f.write_text(
        'from pathlib import Path\n'
        'def f(cfg):\n    return cfg["vault_root"] / "sub"\n',
        encoding="utf-8",
    )

    result = dev_commit.pii_check(files=["ok.py"])
    assert result["clean"] is True
    assert result["hits"] == []


def test_pii_check_skips_binary_extensions(tmp_path, monkeypatch):
    """Binary files should be skipped silently, not decode-error."""
    monkeypatch.setattr("work_buddy.dev.commit.repo_root", lambda: tmp_path)
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nbinary-gibberish")

    result = dev_commit.pii_check(files=["image.png"])
    assert result["clean"] is True
    assert "image.png" not in result["files_scanned"]


# ---------------------------------------------------------------------------
# transient_check — diff-scoped durable-surfaces gate
# ---------------------------------------------------------------------------


def test_transient_check_flags_stage_labels_and_dates(tmp_path, monkeypatch):
    from work_buddy.dev.commit import transient_check
    monkeypatch.setattr("work_buddy.dev.commit.repo_root", lambda: tmp_path)
    f = tmp_path / "module.py"
    f.write_text(
        '"""Slice 5a: resolution layer.\n\nShipped 2026-04-30.\n"""\n',
        encoding="utf-8",
    )
    result = transient_check(files=["module.py"])
    cats = {h["category"] for h in result["hits"]}
    # one hit per line (pii_check convention), so the two categories sit
    # on separate fixture lines
    assert "stage_label" in cats
    assert "date" in cats
    assert result["clean"] is False


def test_transient_check_flags_identifier_form(tmp_path, monkeypatch):
    from work_buddy.dev.commit import transient_check
    monkeypatch.setattr("work_buddy.dev.commit.repo_root", lambda: tmp_path)
    f = tmp_path / "store.py"
    f.write_text("_SLICE_2_COLUMNS = []\n", encoding="utf-8")
    result = transient_check(files=["store.py"])
    assert [h["category"] for h in result["hits"]] == ["stage_label_ident"]


def test_transient_check_suppresses_fixture_data_in_tests(tmp_path, monkeypatch):
    from work_buddy.dev.commit import transient_check
    monkeypatch.setattr("work_buddy.dev.commit.repo_root", lambda: tmp_path)
    tdir = tmp_path / "tests"
    tdir.mkdir()
    f = tdir / "test_foo.py"
    f.write_text(
        'created = "2026-05-01"\ntask_id = "t-a3f8c1e2"\n# Slice 4 shipped this\n',
        encoding="utf-8",
    )
    result = transient_check(files=["tests/test_foo.py"])
    cats = [h["category"] for h in result["hits"]]
    # date + task_ref suppressed in tests; stage_label still fires
    assert cats == ["stage_label"]


def test_transient_check_skips_journal_shape_files(tmp_path, monkeypatch):
    from work_buddy.dev.commit import transient_check
    monkeypatch.setattr("work_buddy.dev.commit.repo_root", lambda: tmp_path)
    f = tmp_path / "CHANGELOG.md"
    f.write_text("## 2026-07-08 - Slice 4 shipped\n", encoding="utf-8")
    result = transient_check(files=["CHANGELOG.md"])
    assert result["files_scanned"] == []
    assert result["clean"] is True


def test_transient_check_clean_file(tmp_path, monkeypatch):
    from work_buddy.dev.commit import transient_check
    monkeypatch.setattr("work_buddy.dev.commit.repo_root", lambda: tmp_path)
    f = tmp_path / "clean.py"
    f.write_text(
        '"""The resolver consults the context registry."""\nTIMEOUT = 30\n',
        encoding="utf-8",
    )
    result = transient_check(files=["clean.py"])
    assert result["clean"] is True
    assert result["files_scanned"] == ["clean.py"]
