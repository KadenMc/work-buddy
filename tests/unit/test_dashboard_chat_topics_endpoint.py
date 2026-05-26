"""Snapshot regression tests for `GET /api/chats/<sid>/topics`.

Locks the JSON shape the dashboard's topics endpoint emits. The
endpoint composes its response from a Python adapter; this test
asserts the response keys + value types so a refactor of the adapter
cannot silently change what the frontend sees.

The test seeds a deterministic fixture summary directly into a tmp
`summarization.db`, monkey-patches `db_path()`, and verifies the
endpoint emits the expected JSON shape.
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
    """Redirect `summarization.db` to a tmp file + drop the lazy singleton
    so the binding factory picks up the new path."""
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


@pytest.fixture
def client():
    from work_buddy.dashboard.service import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _prov(prompt_version: int = 1) -> Provenance:
    return Provenance(
        model="claude-haiku-4-5",
        backend="anthropic",
        profile=None,
        generated_at=Provenance.now_iso(),
        prompt_version=prompt_version,
        summary_schema_version=1,
        selection_version=1,
        cache_version=1,
    )


def _seed_session(
    session_id: str,
    tldr: str,
    topics: list[tuple[str, str, tuple[int, int], list[str]]],
) -> None:
    """Seed one session summary with the given topics into the durable
    store. Each topic is (title, summary, (span_start, span_end), keywords)."""
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
# Snapshot — happy path
# ---------------------------------------------------------------------------


def test_chat_topics_endpoint_returns_full_legacy_shape(
    tmp_summarization_db, client,
):
    _seed_session(
        "sess-snapshot-1",
        "Overall TLDR for the snapshot test.",
        [
            ("First Topic", "First topic body.", (0, 5), ["alpha", "beta"]),
            ("Second Topic", "Second topic body.", (5, 10), ["gamma"]),
        ],
    )

    resp = client.get("/api/chats/sess-snapshot-1/topics")
    assert resp.status_code == 200
    data = resp.get_json()

    assert set(data.keys()) == {"topics", "tldr"}
    assert data["tldr"] == "Overall TLDR for the snapshot test."

    topics = data["topics"]
    assert len(topics) == 2

    # First topic — exact key set + values
    t0 = topics[0]
    assert set(t0.keys()) == {
        "id", "session_id", "topic_index", "title", "summary",
        "span_start", "span_end", "turn_start", "turn_end", "keywords",
    }
    assert t0["id"] == "sess-snapshot-1:0"
    assert t0["session_id"] == "sess-snapshot-1"
    assert t0["topic_index"] == 0
    assert t0["title"] == "First Topic"
    assert t0["summary"] == "First topic body."
    assert t0["span_start"] == 0
    assert t0["span_end"] == 5
    # turn_start / turn_end depend on the session file existing; for a
    # synthesised session, both are None (best-effort behavior preserved).
    assert t0["turn_start"] is None
    assert t0["turn_end"] is None
    assert t0["keywords"] == ["alpha", "beta"]

    # Second topic — keyset parity
    t1 = topics[1]
    assert set(t1.keys()) == set(t0.keys())
    assert t1["topic_index"] == 1
    assert t1["title"] == "Second Topic"
    assert t1["keywords"] == ["gamma"]


# ---------------------------------------------------------------------------
# Missing session
# ---------------------------------------------------------------------------


def test_chat_topics_endpoint_returns_empty_for_unknown_session(
    tmp_summarization_db, client,
):
    """A session with no summary returns empty topics + None tldr."""
    resp = client.get("/api/chats/sess-does-not-exist/topics")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"topics": [], "tldr": None}


# ---------------------------------------------------------------------------
# Status != 'ok' — tldr should NOT surface
# ---------------------------------------------------------------------------


def test_chat_topics_endpoint_returns_no_tldr_when_status_error(
    tmp_summarization_db, client,
):
    """When the session was recorded as 'error' (transient LLM failure),
    the dashboard hides the tldr — `data["tldr"]` is None.

    Topics from the prior 'ok' run are preserved (per `record_error`
    semantics) so users still see history, but the tldr is gated on
    status='ok'."""
    # First seed a successful run
    _seed_session(
        "sess-err-1",
        "Original TLDR before failure.",
        [("T", "S", (0, 1), [])],
    )
    # Then record an error overlaying it
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    store.record_error("sess-err-1", "LLM call failed", _prov())

    resp = client.get("/api/chats/sess-err-1/topics")
    assert resp.status_code == 200
    data = resp.get_json()
    # Prior topics survive (record_error preserves prior nodes)
    assert len(data["topics"]) == 1
    # But tldr is None because status != 'ok'
    assert data["tldr"] is None


# ---------------------------------------------------------------------------
# Empty topics list
# ---------------------------------------------------------------------------


def test_chat_topics_endpoint_returns_zero_topics_for_topicless_session(
    tmp_summarization_db, client,
):
    _seed_session(
        "sess-empty-1",
        "TLDR but no topics.",
        [],
    )

    resp = client.get("/api/chats/sess-empty-1/topics")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["topics"] == []
    assert data["tldr"] == "TLDR but no topics."
