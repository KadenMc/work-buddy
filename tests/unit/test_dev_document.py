"""scan_changes must deterministically classify changes and surface
knowledge-store candidates that textually reference them.

The workflow's intelligence is in the agent, but this step is where
the ground-truth "what changed / what already mentions it" lives. We
pin its shape and matching semantics so a regression (e.g., bucket
reshuffle, __init__.py noise returning) gets caught.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.dev import document as dev_document


@pytest.fixture
def fake_git():
    """Patch _run_git so tests don't depend on the current working tree."""

    state = {"tracked": [], "untracked": []}

    def _fake(*args: str) -> list[str]:
        if args[:1] == ("diff",):
            return state["tracked"]
        if args[:1] == ("ls-files",):
            return state["untracked"]
        return []

    with patch.object(dev_document, "_run_git", side_effect=_fake):
        yield state


def test_classify_covers_all_buckets():
    assert dev_document._classify("work_buddy/mcp_server/x.py") == "module"
    assert dev_document._classify("knowledge/store/dev.json") == "knowledge"
    assert dev_document._classify(".claude/commands/wb-foo.md") == "slash"
    assert dev_document._classify("tests/unit/test_x.py") == "tests"
    assert dev_document._classify("pyproject.toml") == "config"
    assert dev_document._classify("some/thing.unknown") == "other"


def test_subsystem_slugs_from_module_path():
    slugs = dev_document._subsystem_slugs(
        "work_buddy/obsidian/tasks/namespace_suggest.py"
    )
    # Accumulates intermediate dirs + leaf stem
    assert "obsidian" in slugs
    assert "obsidian/tasks" in slugs
    assert "namespace_suggest" in slugs
    # __init__ stem is excluded (too noisy, matches every package)
    slugs_init = dev_document._subsystem_slugs("work_buddy/dev/__init__.py")
    assert "dev" in slugs_init
    assert "__init__" not in slugs_init


def test_subsystem_slugs_from_slash_command():
    slugs = dev_document._subsystem_slugs(".claude/commands/wb-dev-document.md")
    # The `wb-` prefix is stripped so the slug matches the underlying workflow
    assert "dev-document" in slugs
    assert "wb-dev-document" in slugs


def test_scan_changes_empty_returns_warning(fake_git):
    fake_git["tracked"] = []
    fake_git["untracked"] = []
    result = dev_document.scan_changes()
    assert result["changed_files"] == []
    assert result["candidate_units"] == []
    assert any("No changes" in w for w in result["warnings"])


def test_scan_changes_classifies_mixed_input(fake_git):
    fake_git["tracked"] = [
        "work_buddy/dev/document.py",
        "knowledge/store/dev.json",
        "tests/unit/test_foo.py",
    ]
    fake_git["untracked"] = [".claude/commands/wb-dev-document.md"]

    result = dev_document.scan_changes()
    assert "work_buddy/dev/document.py" in result["classified"]["module"]
    assert "knowledge/store/dev.json" in result["classified"]["knowledge"]
    assert "tests/unit/test_foo.py" in result["classified"]["tests"]
    assert ".claude/commands/wb-dev-document.md" in result["classified"]["slash"]
    # Hand-edit warning fires when knowledge/ bucket is non-empty
    assert any("hand-edit" in w.lower() or "docs_create" in w for w in result["warnings"])


def test_scan_changes_surfaces_candidate_units(fake_git):
    """A knowledge unit mentioning a changed module should surface as a
    candidate, and __init__.py alone should not. This is the guardrail
    that keeps the agent from being buried in false positives."""
    # Edit to namespace_suggest — there IS a real knowledge unit
    # (tasks/namespace-suggest... or similar) that mentions this file.
    fake_git["tracked"] = [
        "work_buddy/obsidian/tasks/namespace_suggest.py",
    ]
    fake_git["untracked"] = []
    result = dev_document.scan_changes()
    # We don't pin the exact unit list (the store evolves); we pin the
    # invariant that at least ONE candidate is surfaced for a path that
    # IS referenced in the store, ordered by match strength.
    assert result["candidate_units"], (
        "Expected at least one candidate_unit for a path that the knowledge "
        "store references by name. If this starts failing, either the store "
        "has drifted away from referencing namespace_suggest.py (in which "
        "case pick a different path), or the matcher regressed."
    )
    # Ranking: stronger matches (more `matched_on` tokens) first
    counts = [len(c["matched_on"]) for c in result["candidate_units"]]
    assert counts == sorted(counts, reverse=True)


def test_scan_changes_filters_init_py_noise(fake_git):
    """Changing only __init__.py should not match the universe on the
    bare filename (that would pull in every package's knowledge unit)."""
    fake_git["tracked"] = ["work_buddy/dev/__init__.py"]
    fake_git["untracked"] = []
    result = dev_document.scan_changes()

    # Any matched_on tokens from candidates should NOT be "__init__.py".
    for cand in result["candidate_units"]:
        assert "__init__.py" not in cand["matched_on"], cand
