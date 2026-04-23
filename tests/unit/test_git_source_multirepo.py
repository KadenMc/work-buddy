"""Multi-repo walking in ``context/sources/git.py``.

Replaces the former ``test_journal_collect_multirepo.py``: that test
covered a workaround in ``journal.py`` that directly invoked the
legacy ``collectors.git_collector``. With GitSource now multi-repo,
the workaround is gone and this file exercises the real behavior.

Covered:
- Discovery walks every ``.git`` subdir at depth 1 under ``repos_root``.
- ``custom["repo_path"]`` forces single-repo scope.
- ``dirty_only`` skips clean repos.
- ``include_status`` (or ``dirty_only``) attaches working-tree metadata.
- ``session_map`` annotates commits.
- ``is_stale`` returns True when any repo's HEAD moves.
- Legacy single-repo cache shape is upgraded gracefully.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import work_buddy.context.sources.git as git_src
from work_buddy.context.types import ContextDepth, ContextRequest, ContextSection


# ---------------------------------------------------------------------------
# Fake git layout
# ---------------------------------------------------------------------------


def _init_repo(path: Path, commits: list[str], *, dirty_filename: str | None = None) -> None:
    """Create a working git repo with one commit per entry in ``commits``."""
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=path, check=True
    )
    for i, subject in enumerate(commits):
        (path / f"f{i}.txt").write_text(str(i))
        subprocess.run(["git", "add", "."], cwd=path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", subject], cwd=path, check=True
        )
    if dirty_filename:
        (path / dirty_filename).write_text("uncommitted")


@pytest.fixture
def repos_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    _init_repo(root / "work-buddy", ["feat(wb): first", "fix(wb): second"])
    _init_repo(root / "electricrag", ["feat(er): ablation setup", "Align Full vs Classify"])
    _init_repo(root / "aexp", ["feat(aexp): --dev flag"], dirty_filename="WIP.md")

    # Point config at our fake repos_root.
    from work_buddy import config as cfg_mod
    monkeypatch.setattr(
        cfg_mod, "load_config", lambda config_path=None: {"repos_root": str(root)}
    )
    return root


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def test_collect_walks_every_repo(repos_root):
    src = git_src.GitSource()
    section = src.collect(ContextRequest(window_days=30))
    projects = {c["project"] for c in section.items}
    assert projects == {"work-buddy", "electricrag", "aexp"}
    assert section.metadata["total_repos"] == 3
    assert section.metadata["total_count"] >= 5


def test_collect_forces_single_repo_when_custom_path_given(repos_root):
    src = git_src.GitSource()
    section = src.collect(
        ContextRequest(
            window_days=30,
            custom={"git": {"repo_path": str(repos_root / "electricrag")}},
        )
    )
    projects = {c["project"] for c in section.items}
    assert projects == {"electricrag"}
    assert section.metadata["total_repos"] == 1


def test_collect_dirty_only_filters_clean_repos(repos_root):
    src = git_src.GitSource()
    section = src.collect(
        ContextRequest(window_days=30, custom={"git": {"dirty_only": True}})
    )
    # Only aexp is dirty (WIP.md unstaged).
    repos = [m["project"] for m in section.metadata["repos"]]
    assert repos == ["aexp"]
    # dirty_only implies include_status — dirty_files count must be present.
    assert section.metadata["repos"][0]["dirty_files"] >= 1


def test_collect_session_map_annotates_commits(repos_root):
    src = git_src.GitSource()

    # Seed a session_map using the first commit's short sha from each repo.
    section = src.collect(ContextRequest(window_days=30))
    first_short = section.items[0]["short"]
    session_map = {first_short[:7]: "abcdef1234567890"}

    section2 = src.collect(
        ContextRequest(window_days=30, custom={"git": {"session_map": session_map}})
    )
    hits = [c for c in section2.items if c.get("agent_session")]
    assert hits, "at least one commit should be annotated"
    assert all(c["agent_session"] == "abcdef12" for c in hits)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_buckets_by_project(repos_root):
    src = git_src.GitSource()
    section = src.collect(ContextRequest(window_days=30))
    md = src.render(section, ContextDepth.NORMAL)
    assert "#### work-buddy" in md
    assert "#### electricrag" in md
    assert "#### aexp" in md
    assert "Recent Commits" in md


def test_render_empty_commits_but_dirty_shows_dirty_note(repos_root):
    src = git_src.GitSource()
    # Window too narrow to see any commits.
    from datetime import date
    section = src.collect(
        ContextRequest(
            window_days=0,
            target_date=date(1970, 1, 1),
            custom={"git": {"dirty_only": True}},
        )
    )
    md = src.render(section, ContextDepth.NORMAL)
    assert "aexp" in md
    assert "dirty" in md.lower()


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


def test_is_stale_detects_head_move(repos_root):
    src = git_src.GitSource()
    req = ContextRequest(window_days=30)
    section = src.collect(req)
    assert src.is_stale(section, req) is False

    # Advance one repo's HEAD.
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "extra"],
        cwd=repos_root / "electricrag",
        check=True,
    )
    assert src.is_stale(section, req) is True


def test_is_stale_handles_legacy_single_repo_cache(repos_root):
    src = git_src.GitSource()
    legacy_cached = ContextSection(
        source="git",
        items=[],
        metadata={"repo": str(repos_root / "work-buddy"), "head": "deadbeef" * 5},
    )
    req = ContextRequest(
        window_days=30,
        custom={"git": {"repo_path": str(repos_root / "work-buddy")}},
    )
    # Stale HEAD in metadata → must invalidate.
    assert src.is_stale(legacy_cached, req) is True


# ---------------------------------------------------------------------------
# drill_down
# ---------------------------------------------------------------------------


def test_drill_down_finds_commit_in_any_repo(repos_root):
    src = git_src.GitSource()
    section = src.collect(ContextRequest(window_days=30))
    # Pick a commit from the electricrag repo.
    target = next(c for c in section.items if c["project"] == "electricrag")

    result = src.drill_down(target["short"], "full_message")
    assert result["repo"] == "electricrag"
    assert "message" in result


def test_drill_down_raises_on_unknown_commit(repos_root):
    src = git_src.GitSource()
    with pytest.raises(KeyError):
        src.drill_down("ffffffffff", "full_message")
