"""Tests for `work_buddy.ir.store._get_source` factory dispatch.

Phase 3d refactored the if/elif chain to a small factory dict. These
tests verify each source name resolves to the expected adapter class,
unknown sources raise a clear error, and the docs source remains
intact as the knowledge-store search path.
"""

from __future__ import annotations

import pytest

from work_buddy.ir.store import _get_source


def test_all_known_sources_resolve():
    """Every source name documented in the public API should resolve to
    a concrete adapter instance."""
    expected = {
        "conversation": "ConversationSource",
        "chrome": "ChromeSource",
        "projects": "ProjectsSource",
        "docs": "DocsSource",
        "task_note": "TaskNoteSource",
        "summary": "SummarySource",
    }
    for source_name, class_name in expected.items():
        adapter = _get_source(source_name)
        assert type(adapter).__name__ == class_name, (
            f"source {source_name!r} resolved to {type(adapter).__name__}, "
            f"expected {class_name}"
        )


def test_unknown_source_raises_with_available_list():
    with pytest.raises(ValueError, match="Unknown source"):
        _get_source("not_a_real_source")


def test_unknown_source_error_lists_known_sources():
    """The error message must list available sources so the caller can
    correct their input without diving into the implementation."""
    try:
        _get_source("zzz")
    except ValueError as exc:
        msg = str(exc)
        for known in ("conversation", "docs", "summary", "task_note"):
            assert known in msg, f"available list missing {known!r}: {msg}"
    else:
        pytest.fail("expected ValueError")


def test_docs_source_emits_with_correct_name():
    """The docs source is the knowledge-store IR source. Its `name`
    property MUST be 'docs' so the engine stores documents under that
    source filter — this is how `find(source=\"docs\", query=...)`
    works to search the knowledge store."""
    adapter = _get_source("docs")
    assert adapter.name == "docs"


def test_each_call_returns_a_fresh_adapter_instance():
    """Factory pattern — each invocation produces a new instance. This
    matters when the adapter's state (e.g., cached config) needs to be
    independent across callers."""
    a = _get_source("docs")
    b = _get_source("docs")
    assert a is not b  # different instances
    assert type(a) is type(b)  # same class
