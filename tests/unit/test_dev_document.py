"""scan_changes must deterministically classify changes and surface
knowledge-store candidates relevant to them.

The workflow's intelligence is in the agent, but this step is where
the ground-truth "what changed / what already mentions it" lives. We
pin its shape (per-candidate keys, _source flag) and the matching
contract (RAG-first, grep-fallback, force-include for canonical entry
points), so a regression gets caught.
"""

from __future__ import annotations

from typing import Any
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


def test_module_paths_from_changed_basics():
    """The dotted-path derivation for force-include matching."""
    paths = dev_document._module_paths_from_changed([
        "work_buddy/dashboard/forms.py",
        "work_buddy/dev/__init__.py",
        "tests/unit/test_x.py",  # not under work_buddy/, ignored
    ])
    assert "work_buddy.dashboard.forms" in paths
    # __init__.py rolls up to the package
    assert "work_buddy.dev" in paths
    assert not any("test_x" in p for p in paths)


# ---------------------------------------------------------------------------
# scan_changes — basic shape
# ---------------------------------------------------------------------------

def test_scan_changes_empty_returns_warning(fake_git):
    fake_git["tracked"] = []
    fake_git["untracked"] = []
    result = dev_document.scan_changes()
    assert result["changed_files"] == []
    assert result["candidate_units"] == []
    assert any("No changes" in w for w in result["warnings"])
    # _source is set even on empty results so we know which path ran
    assert result["_source"] in ("rag", "grep_fallback")


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


def test_scan_candidates_shape_is_slimmed(fake_git):
    """Each candidate must be the slim shape: {path, name, description, score, why}."""
    fake_git["tracked"] = ["work_buddy/dev/document.py"]
    fake_git["untracked"] = []
    result = dev_document.scan_changes()
    # If any candidates surface, each one has the expected slim keys.
    for cand in result["candidate_units"]:
        assert set(cand.keys()) >= {"path", "name", "description", "score", "why"}, cand
        # Sanity: no full-content fields leaking through.
        assert "matched_on" not in cand
        assert "kind" not in cand or cand.get("kind") in (None, "")  # tolerated, not required
    # Cap honored.
    assert len(result["candidate_units"]) <= 20


# ---------------------------------------------------------------------------
# RAG vs grep dispatch
# ---------------------------------------------------------------------------

def _fake_search_success(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """search() stub that returns a deterministic small result set."""
    return {
        "mode": "search",
        "query": kwargs.get("query", ""),
        "count": 2,
        "results": [
            {
                "path": "services/dashboard",
                "name": "Dashboard",
                "description": "Dashboard service.",
                "score": 0.42,
                "kind": "service",
            },
            {
                "path": "architecture/event-bus",
                "name": "Event Bus",
                "description": "SSE pub-sub.",
                "score": 0.31,
                "kind": "system",
            },
        ],
    }


def _fake_search_raises(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """search() stub that simulates an embedding-service failure."""
    raise RuntimeError("embedding service unhealthy")


def test_scan_uses_rag_when_available(fake_git):
    """When search() returns scored hits, the candidates carry _source: rag.

    The scan now makes 1 + N search() calls (one structural query, plus one
    per Python file with a docstring). The mock's ``side_effect`` returns
    the same response for every call.
    """
    fake_git["tracked"] = ["work_buddy/dashboard/forms.py"]
    fake_git["untracked"] = []
    # Stub _read_module_docstring to be deterministic — return empty so this
    # test exercises the structural-query-only path.
    with patch("work_buddy.knowledge.search.search", side_effect=_fake_search_success), \
         patch("work_buddy.dev.document._read_module_docstring", return_value=""):
        result = dev_document.scan_changes()
    assert result["_source"] == "rag"
    # The fake hits surface in the candidate list.
    paths = {c["path"] for c in result["candidate_units"]}
    assert "services/dashboard" in paths
    # Each candidate has a non-empty `why` describing the match.
    for cand in result["candidate_units"]:
        assert cand["why"]
        # The new fused-source label is informative.
        assert "fused" in cand["why"] or "matched" in cand["why"]


def test_scan_falls_back_on_search_failure(fake_git):
    """When search() raises on the structural query, scan falls back to grep."""
    fake_git["tracked"] = ["work_buddy/obsidian/tasks/namespace_suggest.py"]
    fake_git["untracked"] = []
    with patch("work_buddy.knowledge.search.search", side_effect=_fake_search_raises):
        result = dev_document.scan_changes()
    assert result["_source"] == "grep_fallback"
    # The fallback should still surface SOME candidates against the real
    # store (which references namespace_suggest in some unit's prose).
    # We don't pin the count — just that the response shape is correct.
    for cand in result["candidate_units"]:
        assert set(cand.keys()) >= {"path", "name", "description", "score", "why"}


def test_scan_caps_at_20_candidates(fake_git):
    """A flood of fused search hits gets truncated to top 20."""
    many = [
        {
            "path": f"fake/unit-{i}",
            "name": f"Fake {i}",
            "description": "x",
            "score": 1.0 - (i * 0.01),
        }
        for i in range(50)
    ]
    fake = {"mode": "search", "query": "x", "count": 50, "results": many}
    fake_git["tracked"] = ["work_buddy/dev/document.py"]
    fake_git["untracked"] = []
    with patch("work_buddy.knowledge.search.search", return_value=fake), \
         patch("work_buddy.dev.document._read_module_docstring", return_value=""):
        result = dev_document.scan_changes()
    assert result["_source"] == "rag"
    assert len(result["candidate_units"]) <= 20


# ---------------------------------------------------------------------------
# Multi-query + RRF fusion
# ---------------------------------------------------------------------------

def test_scan_fuses_path_and_docstring_signals(fake_git):
    """A unit appearing in BOTH the structural and docstring rankings ranks
    higher than a unit that appears in only one.

    This is the property that makes RRF fusion the right tool — equal voice
    across signal sources, regardless of query length.
    """
    # First call (structural query) returns A, B (in that order).
    # Second call (docstring query) returns B, C.
    # B appears in BOTH → should fuse to rank 1.
    structural = {
        "mode": "search", "count": 2,
        "results": [
            {"path": "fake/A", "name": "A", "description": "a", "score": 0.5},
            {"path": "fake/B", "name": "B", "description": "b", "score": 0.4},
        ],
    }
    docstring = {
        "mode": "search", "count": 2,
        "results": [
            {"path": "fake/B", "name": "B", "description": "b", "score": 0.5},
            {"path": "fake/C", "name": "C", "description": "c", "score": 0.4},
        ],
    }
    fake_git["tracked"] = ["work_buddy/dev/document.py"]
    fake_git["untracked"] = []
    with patch(
        "work_buddy.knowledge.search.search",
        side_effect=[structural, docstring],
    ), patch(
        "work_buddy.dev.document._read_module_docstring",
        return_value="some docstring text",
    ):
        result = dev_document.scan_changes()
    paths = [c["path"] for c in result["candidate_units"]]
    assert paths[0] == "fake/B", f"shared hit should rank first; got {paths}"
    # B's `why` should mention both sources.
    b_hit = next(c for c in result["candidate_units"] if c["path"] == "fake/B")
    assert "paths" in b_hit["why"] and "docstring" in b_hit["why"]


def test_scan_skips_files_without_docstrings(fake_git):
    """Files with no docstring contribute no extra search call."""
    structural = {
        "mode": "search", "count": 1,
        "results": [{"path": "fake/A", "name": "A", "description": "a", "score": 0.9}],
    }
    fake_git["tracked"] = ["work_buddy/dev/document.py"]
    fake_git["untracked"] = []
    mock_search = patch(
        "work_buddy.knowledge.search.search",
        return_value=structural,
    )
    with mock_search as m, patch(
        "work_buddy.dev.document._read_module_docstring",
        return_value="",
    ):
        dev_document.scan_changes()
    # Only the structural query should have run.
    assert m.call_count == 1, (
        f"Expected 1 search call (structural only); got {m.call_count}"
    )


# ---------------------------------------------------------------------------
# _read_module_docstring
# ---------------------------------------------------------------------------

def test_read_module_docstring_extracts_first_paragraph():
    """Real Python module: returns the first paragraph of its docstring."""
    # work_buddy/dev/document.py has a real top-of-file docstring.
    out = dev_document._read_module_docstring("work_buddy/dev/document.py")
    assert out, "expected non-empty docstring"
    # First paragraph stops at the first blank line — should be a single
    # paragraph, no double-newlines inside.
    assert "\n\n" not in out
    # Domain words from the actual docstring.
    assert "scan" in out.lower() or "knowledge" in out.lower()


def test_read_module_docstring_returns_empty_for_non_python():
    """Non-.py files (markdown, JSON, etc.) get the empty string."""
    assert dev_document._read_module_docstring("README.md") == ""
    assert dev_document._read_module_docstring("knowledge/store/dev.json") == ""


def test_read_module_docstring_returns_empty_for_unreadable():
    """Missing files don't crash — caller gets empty string."""
    out = dev_document._read_module_docstring("does/not/exist.py")
    assert out == ""


def test_read_module_docstring_caps_at_max_chars():
    """Very long docstrings get truncated to max_chars."""
    # Use a real file but truncate aggressively.
    out = dev_document._read_module_docstring(
        "work_buddy/dev/document.py", max_chars=20
    )
    assert len(out) <= 20
