"""Tests for `work_buddy.conversation_observability.session_summary_row`.

The module owns the canonical `session_summaries` + `topic_summaries`
row shape. The shim `summaries.query_session_summary` produces the same
dict from the same store. These tests assert the two paths stay
byte-equivalent and verify the row's invariants (key set, missing-session
handling, error-status preservation, topics without span metadata).
"""

from __future__ import annotations

import time

import pytest

from work_buddy.summarization import Provenance, SummaryNode
from work_buddy.summarization.stores import DurableSummaryStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_summarization_db(monkeypatch, tmp_path):
    from work_buddy.summarization import db as db_mod
    from work_buddy.conversation_observability.summarizer_binding import (
        reset_session_summarizer,
    )

    db_file = tmp_path / "summarization.db"
    monkeypatch.setattr(db_mod, "_default_db_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "db_path", lambda cfg=None: db_file)
    reset_session_summarizer()
    yield db_file
    reset_session_summarizer()


def _prov() -> Provenance:
    return Provenance(
        model="claude-haiku-4-5",
        backend="anthropic",
        profile=None,
        generated_at=Provenance.now_iso(),
        prompt_version=1,
        summary_schema_version=1,
        selection_version=1,
        cache_version=1,
    )


def _seed(session_id: str, tldr: str, topics):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    children = [
        SummaryNode(
            summary=summary,
            source_ref={"span_start": span[0], "span_end": span[1]},
            extra={
                "title": title,
                "topic_index": i,
                "keywords": list(kw),
                "span_start": span[0],
                "span_end": span[1],
            },
        )
        for i, (title, summary, span, kw) in enumerate(topics)
    ]
    root = SummaryNode(summary=tldr, source_ref=None, children=children)
    store.save(session_id, root, _prov(), str(time.time()))


# ---------------------------------------------------------------------------
# Equivalence with the compatibility shim
# ---------------------------------------------------------------------------


def test_session_summary_row_matches_query_session_summary_happy_path(
    tmp_summarization_db,
):
    _seed(
        "sess-eq-1",
        "TLDR for equivalence test.",
        [
            ("A", "Body A.", (0, 5), ["alpha"]),
            ("B", "Body B.", (5, 10), ["beta", "gamma"]),
        ],
    )

    from work_buddy.conversation_observability.session_summary_row import (
        session_summary_row,
    )
    from work_buddy.conversation_observability.summaries import (
        query_session_summary,
    )

    shim = query_session_summary("sess-eq-1")
    canonical = session_summary_row("sess-eq-1")

    assert shim == canonical


def test_session_summary_row_returns_none_for_missing_session(
    tmp_summarization_db,
):
    from work_buddy.conversation_observability.session_summary_row import (
        session_summary_row,
    )
    from work_buddy.conversation_observability.summaries import (
        query_session_summary,
    )

    shim = query_session_summary("never-existed")
    canonical = session_summary_row("never-existed")
    assert shim is None
    assert canonical is None
    assert shim == canonical


def test_session_summary_row_matches_for_error_status(tmp_summarization_db):
    """When `record_error` overlays an existing session, the row helper
    should still surface the prior tldr+topics (not the error)."""
    _seed("sess-err", "Original TLDR.", [("T", "S", (0, 1), [])])
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    store.record_error("sess-err", "boom", _prov())

    from work_buddy.conversation_observability.session_summary_row import (
        session_summary_row,
    )
    from work_buddy.conversation_observability.summaries import (
        query_session_summary,
    )

    assert query_session_summary("sess-err") == session_summary_row("sess-err")


def test_session_summary_row_includes_all_thirteen_fields(
    tmp_summarization_db,
):
    _seed("sess-13", "tldr", [("T", "S", (0, 1), [])])

    from work_buddy.conversation_observability.session_summary_row import (
        session_summary_row,
    )

    row = session_summary_row("sess-13")
    assert set(row.keys()) == {
        "session_id", "tldr", "topic_count", "generated_at", "model",
        "profile", "backend", "prompt_version", "summary_schema_version",
        "selection_version", "cache_version", "status", "error", "topics",
    }


def test_session_topics_helper_matches_topic_list(tmp_summarization_db):
    _seed(
        "sess-topics", "tldr",
        [("T1", "S1", (0, 3), ["x"]), ("T2", "S2", (3, 6), ["y"])],
    )

    from work_buddy.conversation_observability.session_summary_row import (
        session_summary_row,
        session_topics,
    )

    row = session_summary_row("sess-topics")
    topics_only = session_topics("sess-topics")
    assert topics_only == row["topics"]


def test_session_topics_helper_returns_empty_for_missing(
    tmp_summarization_db,
):
    from work_buddy.conversation_observability.session_summary_row import (
        session_topics,
    )

    assert session_topics("does-not-exist") == []


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_topics_without_span_metadata_returns_none_turn_range(
    tmp_summarization_db,
):
    """A topic node missing span metadata should still appear in the
    output with `turn_start`/`turn_end` = None, not crash."""
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    children = [
        SummaryNode(
            summary="orphan body",
            source_ref=None,
            extra={"title": "No spans", "keywords": []},
        ),
    ]
    root = SummaryNode(
        summary="parent tldr", source_ref=None, children=children,
    )
    store.save("sess-no-span", root, _prov(), str(time.time()))

    from work_buddy.conversation_observability.session_summary_row import (
        session_topics,
    )

    topics = session_topics("sess-no-span")
    assert len(topics) == 1
    assert topics[0]["span_start"] is None
    assert topics[0]["span_end"] is None
    assert topics[0]["turn_start"] is None
    assert topics[0]["turn_end"] is None
