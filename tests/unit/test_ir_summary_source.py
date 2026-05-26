"""Tests for `work_buddy.ir.sources.summary.SummarySource`.

The source reads `summary_items` + `summary_nodes` from the summarization
framework's durable store and emits per-node IR Documents. Tests verify:
discovery filters by recency + status, parse emits one Document per node
with the right fields/metadata, and end-to-end through `_get_source` /
`build_index` registers the source under the name `summary`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from work_buddy.ir.sources.summary import SummarySource
from work_buddy.summarization import Provenance, SummaryNode
from work_buddy.summarization.stores import DurableSummaryStore


@pytest.fixture
def tmp_summarization_db(monkeypatch, tmp_path):
    """Point summarization.db at a tmp file."""
    from work_buddy.summarization import db as db_mod

    db_file = tmp_path / "summarization.db"
    monkeypatch.setattr(db_mod, "_default_db_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "db_path", lambda cfg=None: db_file)
    return db_file


def _prov(prompt_v: int = 1, schema_v: int = 1) -> Provenance:
    return Provenance(
        model="claude-haiku-4-5", backend="anthropic", profile=None,
        generated_at=Provenance.now_iso(),
        prompt_version=prompt_v, summary_schema_version=schema_v,
        selection_version=1, cache_version=1,
    )


def _seed_layered(store: DurableSummaryStore, item_id: str, tldr: str,
                  topics: list[tuple[str, str, list[int], list[str]]]) -> None:
    """Save a layered node tree (root + topic children) under the store."""
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
    store.save(item_id, root, _prov(), str(time.time()))


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


def test_discover_returns_recent_ok_items(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(store, "sess-a", "tldr A", [("T1", "S1", (0, 5), ["k"])])
    _seed_layered(store, "sess-b", "tldr B", [("T2", "S2", (0, 3), ["k"])])

    src = SummarySource()
    found = src.discover(days=30)

    ids = sorted(iid for iid, _ in found)
    assert ids == ["conversation_session:sess-a", "conversation_session:sess-b"]
    # mtime is a positive float (parsed from generated_at).
    for _, m in found:
        assert isinstance(m, float) and m > 0


def test_discover_filters_by_status(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(store, "ok-sess", "good tldr", [("T", "S", (0, 1), [])])
    # An error-only row (no nodes) — should NOT appear in discover().
    store.record_error("err-sess", "boom", _prov())

    src = SummarySource()
    found_ids = {iid for iid, _ in src.discover(days=30)}
    assert "conversation_session:ok-sess" in found_ids
    assert "conversation_session:err-sess" not in found_ids


def test_discover_returns_empty_when_db_absent(tmp_path, monkeypatch):
    """If summarization.db hasn't been created yet, discover returns []."""
    from work_buddy.summarization import db as db_mod
    missing = tmp_path / "never-created.db"
    monkeypatch.setattr(db_mod, "_default_db_path", lambda: missing)
    monkeypatch.setattr(db_mod, "db_path", lambda cfg=None: missing)

    assert SummarySource().discover(days=30) == []


def test_discover_respects_days_window(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)

    # Insert one row, then manually backdate its generated_at well outside
    # the default window so discover() drops it.
    _seed_layered(store, "old-sess", "old", [("T", "S", (0, 1), [])])
    import sqlite3
    conn = sqlite3.connect(str(tmp_summarization_db))
    conn.execute(
        "UPDATE summary_items SET generated_at = ? WHERE item_id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=400)).isoformat(),
         "old-sess"),
    )
    conn.commit()
    conn.close()

    assert SummarySource().discover(days=30) == []
    # Wider window: re-discovered.
    assert any(
        iid == "conversation_session:old-sess"
        for iid, _ in SummarySource().discover(days=500)
    )


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def test_parse_emits_one_document_per_node(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(
        store, "sess-x", "Overall tldr",
        [
            ("First topic", "Worked on X.", (0, 10), ["x", "alpha"]),
            ("Second topic", "Then Y happened.", (10, 15), ["y", "beta"]),
        ],
    )

    docs = SummarySource().parse("conversation_session:sess-x")

    # Three nodes: root + two children.
    assert len(docs) == 3
    # All carry source=summary and the same namespace+item_id metadata.
    for d in docs:
        assert d.source == "summary"
        assert d.metadata["namespace"] == "conversation_session"
        assert d.metadata["item_id"] == "sess-x"

    levels = sorted(d.metadata["level"] for d in docs)
    assert levels == [0, 1, 1]


def test_parse_root_has_empty_title_and_null_source_ref(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(
        store, "sess-r", "root tldr",
        [("Title", "summary text", (0, 5), ["kw"])],
    )

    docs = SummarySource().parse("conversation_session:sess-r")
    root = next(d for d in docs if d.metadata["level"] == 0)

    assert root.fields["title"] == ""
    assert root.fields["summary"] == "root tldr"
    assert root.metadata["source_ref"] is None
    assert root.metadata["parent_id"] is None


def test_parse_children_carry_title_keywords_and_source_ref(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(
        store, "sess-c", "tldr",
        [("Topic Title", "Topic summary body.", (12, 27), ["alpha", "beta"])],
    )

    docs = SummarySource().parse("conversation_session:sess-c")
    child = next(d for d in docs if d.metadata["level"] == 1)

    assert child.fields["title"] == "Topic Title"
    assert child.fields["summary"] == "Topic summary body."
    assert child.fields["keywords"] == "alpha beta"
    # source_ref decoded back to a dict.
    assert child.metadata["source_ref"] == {"span_start": 12, "span_end": 27}
    # The display string previews title + summary.
    assert "Topic Title" in child.display_text


def test_parse_dense_text_combines_title_summary_keywords(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(
        store, "sess-d", "root",
        [("My Title", "My body.", (0, 1), ["one", "two"])],
    )

    docs = SummarySource().parse("conversation_session:sess-d")
    child = next(d for d in docs if d.metadata["level"] == 1)

    for token in ["My Title", "My body.", "one", "two"]:
        assert token in child.dense_text


def test_parse_doc_ids_stable_and_unique(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(
        store, "sess-i", "tldr",
        [("A", "a", (0, 1), []), ("B", "b", (1, 2), [])],
    )

    docs = SummarySource().parse("conversation_session:sess-i")
    ids = [d.doc_id for d in docs]
    assert len(ids) == len(set(ids))  # unique
    # Pattern check: {ns}:{item}:n{ordinal}.
    for d in docs:
        assert d.doc_id.startswith("conversation_session:sess-i:n")


def test_parse_empty_when_item_missing(tmp_summarization_db):
    DurableSummaryStore("conversation_session")  # ensure db exists
    assert SummarySource().parse("conversation_session:nonexistent") == []


def test_parse_rejects_malformed_item_id(tmp_summarization_db):
    """An ir_item_id without a `namespace:inner` split returns []."""
    assert SummarySource().parse("no-colon-here") == []


# ---------------------------------------------------------------------------
# Registration in _get_source
# ---------------------------------------------------------------------------


def test_summary_source_registered_in_get_source():
    from work_buddy.ir.store import _get_source

    src = _get_source("summary")
    assert isinstance(src, SummarySource)
    assert src.name == "summary"
