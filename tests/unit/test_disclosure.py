"""Tests for the disclosure (progressive-disclosure) navigation contract.

Covers:
- `SummaryTreeDrillable` over a tmp summarization.db with seeded tree.
- `KnowledgeTreeDrillable` smoke (against the live store — exercises the
  agent_docs wrapping path).
- `drill_tree` dispatch — domain resolution, error shapes.
- `register_drillable` — registration semantics + reset for tests.
"""

from __future__ import annotations

import time

import pytest

from work_buddy.disclosure import (
    ChildRef,
    DrillError,
    TreeView,
    available_domains,
    get_drillable,
    register_drillable,
)
from work_buddy.disclosure.dispatch import drill_tree
from work_buddy.disclosure.protocol import TreeDrillable
from work_buddy.disclosure.registry import _reset_for_tests
from work_buddy.disclosure.summary_tree import (
    SummaryTreeDrillable,
    _parse_node_id,
)
from work_buddy.summarization import Provenance, SummaryNode
from work_buddy.summarization.stores import DurableSummaryStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_summarization_db(monkeypatch, tmp_path):
    from work_buddy.summarization import db as db_mod

    db_file = tmp_path / "summarization.db"
    monkeypatch.setattr(db_mod, "_default_db_path", lambda: db_file)
    monkeypatch.setattr(db_mod, "db_path", lambda cfg=None: db_file)
    return db_file


def _seed_layered(store, item_id, tldr, topics):
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


def _prov():
    return Provenance(
        model="m", backend="b", profile=None,
        generated_at=Provenance.now_iso(),
        prompt_version=1, summary_schema_version=1,
        selection_version=1, cache_version=1,
    )


# ---------------------------------------------------------------------------
# _parse_node_id
# ---------------------------------------------------------------------------


def test_parse_node_id_item_only():
    ns, inner, ord_ = _parse_node_id("conversation_session:abc")
    assert (ns, inner, ord_) == ("conversation_session", "abc", None)


def test_parse_node_id_with_ordinal():
    ns, inner, ord_ = _parse_node_id("conversation_session:abc#n3")
    assert (ns, inner, ord_) == ("conversation_session", "abc", 3)


def test_parse_node_id_missing_namespace_raises():
    with pytest.raises(DrillError, match="missing namespace"):
        _parse_node_id("just-an-id")


def test_parse_node_id_bad_ordinal_raises():
    with pytest.raises(DrillError, match="ordinal"):
        _parse_node_id("ns:id#nXYZ")


def test_parse_node_id_empty_parts_raises():
    with pytest.raises(DrillError, match="empty"):
        _parse_node_id(":")


# ---------------------------------------------------------------------------
# SummaryTreeDrillable
# ---------------------------------------------------------------------------


def test_summary_get_root_index_lists_topic_children(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(
        store, "sess-x", "Overall tldr",
        [
            ("First topic", "Did X.", (0, 10), ["x"]),
            ("Second topic", "Did Y.", (10, 15), ["y"]),
        ],
    )

    s = SummaryTreeDrillable()
    view = s.get("conversation_session:sess-x", depth="index")

    assert view.domain == "summary"
    assert view.depth == "index"
    assert view.summary_text == "Overall tldr"
    assert view.full_text is None
    assert len(view.children) == 2
    assert view.children[0].title == "First topic"
    # depth=index → children have no summary_text.
    assert view.children[0].summary_text is None
    # child node_id format includes ordinal.
    assert view.children[0].node_id == "conversation_session:sess-x#n1"


def test_summary_get_root_summary_includes_child_summaries(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(
        store, "sess-x", "tldr",
        [("Topic A", "Did A.", (0, 5), [])],
    )

    view = SummaryTreeDrillable().get(
        "conversation_session:sess-x", depth="summary",
    )
    assert view.children[0].summary_text == "Did A."


def test_summary_get_root_full_concats_subtree(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(
        store, "sess-x", "tldr text",
        [
            ("T1", "child1 summary", (0, 5), []),
            ("T2", "child2 summary", (5, 10), []),
        ],
    )

    view = SummaryTreeDrillable().get(
        "conversation_session:sess-x", depth="full",
    )
    assert view.full_text is not None
    # Pre-order: root tldr, T1 header + child1, T2 header + child2.
    for token in [
        "tldr text", "# T1", "child1 summary",
        "# T2", "child2 summary",
    ]:
        assert token in view.full_text


def test_summary_get_specific_child_node(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(
        store, "sess-x", "tldr",
        [
            ("First", "first body", (0, 5), ["a"]),
            ("Second", "second body", (5, 10), ["b"]),
        ],
    )

    view = SummaryTreeDrillable().get(
        "conversation_session:sess-x#n2", depth="summary",
    )
    assert view.title == "Second"
    assert view.summary_text == "second body"
    assert view.parent_id == "conversation_session:sess-x"
    assert view.metadata["source_ref"] == {
        "span_start": 5, "span_end": 10,
    }


def test_summary_missing_item_raises_drillerror(tmp_summarization_db):
    # Seed something else so the DB file exists; then look up an
    # unrelated id.
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(store, "existing", "tldr", [("T", "S", (0, 1), [])])

    s = SummaryTreeDrillable()
    with pytest.raises(DrillError, match="not found"):
        s.get("conversation_session:nope", depth="index")


def test_summary_invalid_depth_raises_drillerror(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(store, "sess-x", "tldr", [("T", "S", (0, 1), [])])

    with pytest.raises(DrillError, match="invalid depth"):
        SummaryTreeDrillable().get(
            "conversation_session:sess-x", depth="deep",
        )


def test_summary_error_status_returns_view_with_no_nodes(tmp_summarization_db):
    """When an item was recorded as an error with no prior nodes, the
    drillable returns a view explaining the state (rather than raising)."""
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    store.record_error("err-sess", "boom", _prov())

    view = SummaryTreeDrillable().get(
        "conversation_session:err-sess", depth="summary",
    )
    assert view.children == []
    assert view.full_text is None
    assert "error" in view.summary_text.lower()
    assert view.metadata["status"] == "error"


# ---------------------------------------------------------------------------
# KnowledgeTreeDrillable — smoke against live store
# ---------------------------------------------------------------------------


def test_knowledge_get_known_unit_index():
    from work_buddy.disclosure.knowledge_tree import KnowledgeTreeDrillable

    k = KnowledgeTreeDrillable()
    view = k.get("architecture/summarization-framework", depth="index")

    assert view.domain == "knowledge"
    assert view.title == "Summarization Framework"
    assert view.summary_text  # non-empty description
    assert view.full_text is None  # depth=index never populates full_text


def test_knowledge_get_full_includes_content():
    from work_buddy.disclosure.knowledge_tree import KnowledgeTreeDrillable

    k = KnowledgeTreeDrillable()
    view = k.get("architecture/summarization-framework", depth="full")

    assert view.full_text is not None
    assert len(view.full_text) > 100   # the unit has substantial content


def test_knowledge_get_missing_raises_drillerror():
    from work_buddy.disclosure.knowledge_tree import KnowledgeTreeDrillable

    k = KnowledgeTreeDrillable()
    with pytest.raises(DrillError, match="not found"):
        k.get("does/not/exist/anywhere", depth="summary")


def test_knowledge_invalid_depth_raises():
    from work_buddy.disclosure.knowledge_tree import KnowledgeTreeDrillable

    k = KnowledgeTreeDrillable()
    with pytest.raises(DrillError, match="invalid depth"):
        k.get("architecture", depth="huge")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_default_registry_contains_summary_and_knowledge():
    domains = available_domains()
    assert "summary" in domains
    assert "knowledge" in domains


def test_register_drillable_swaps_factory():
    class _Fake:
        domain = "fake"

        def get(self, node_id, depth="summary"):
            return TreeView(
                domain="fake", node_id=node_id, title="x", depth=depth,
            )

    register_drillable("fake", lambda: _Fake())
    try:
        inst = get_drillable("fake")
        assert isinstance(inst, _Fake)
    finally:
        # Restore the default registry for subsequent tests.
        _reset_for_tests()
        # Re-register the defaults that this test clobbered.
        from work_buddy.disclosure.registry import _register_defaults
        _register_defaults()


def test_get_drillable_unknown_raises_keyerror():
    with pytest.raises(KeyError, match="No TreeDrillable"):
        get_drillable("nonsuch_domain_xyz")


# ---------------------------------------------------------------------------
# Dispatch (drill_tree)
# ---------------------------------------------------------------------------


def test_dispatch_unknown_domain_returns_structured_error():
    out = drill_tree("nonsense", "some-id", "summary")
    assert "error" in out
    assert "nonsense" in out["error"] or "nonsense" == out.get("domain")
    assert "available_domains" in out


def test_dispatch_propagates_drill_error_as_dict():
    out = drill_tree("summary", "no-colon-anywhere", "summary")
    assert "error" in out
    assert out["domain"] == "summary"
    assert out["node_id"] == "no-colon-anywhere"


def test_dispatch_returns_view_dict_on_success(tmp_summarization_db):
    store = DurableSummaryStore("conversation_session")
    store.set_strategy_versions(1, 1)
    _seed_layered(
        store, "sess-d", "tldr d",
        [("T", "S", (0, 1), [])],
    )

    out = drill_tree(
        "summary", "conversation_session:sess-d", "summary",
    )
    assert out["domain"] == "summary"
    assert out["title"] == "conversation_session:sess-d"
    assert out["summary_text"] == "tldr d"
    assert out["depth"] == "summary"
    assert len(out["children"]) == 1
    assert out["children"][0]["title"] == "T"


def test_dispatch_empty_domain_returns_error():
    out = drill_tree("", "x", "summary")
    assert "error" in out
    assert "domain is required" in out["error"]
