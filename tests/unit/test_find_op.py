"""Tests for `work_buddy.mcp_server.ops.search_ops.find_op`.

The structured-returning universal search verb. Two return modes:
- `drill=False` -> plain `list[dict]` from `ir.search.search`.
- `drill=True` -> funnel-shape dict, with per-source drill handler lookup.

`summary_search` is the existing alias for the funnel shape; these
tests assert parity when callers pass `source="summary"`, `drill=True`
so the two ops stay byte-equivalent.
"""

from __future__ import annotations

from typing import Any

import pytest

from work_buddy.mcp_server.ops.search_ops import find_op


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hit(
    source: str,
    doc_id: str,
    *,
    namespace: str = "",
    item_id: str = "",
    score: float = 0.5,
    title: str = "",
    summary: str = "",
    ordinal: int = 0,
    level: int = 1,
) -> dict[str, Any]:
    """Build a fake ir.search.search result row."""
    return {
        "doc_id": doc_id,
        "score": score,
        "source": source,
        "display_text": summary,
        "metadata": {
            "namespace": namespace,
            "item_id": item_id,
            "level": level,
            "ordinal": ordinal,
            "extra": {"title": title},
        },
    }


@pytest.fixture
def patch_ir_search(monkeypatch):
    """Return a helper that installs a fake `ir.search.search` for the
    duration of the test."""
    def _install(fake):
        import work_buddy.ir.search as ir_search_mod

        monkeypatch.setattr(ir_search_mod, "search", fake)

    return _install


# ---------------------------------------------------------------------------
# drill=False — pass-through list shape
# ---------------------------------------------------------------------------


def test_find_drill_false_returns_raw_ir_list(patch_ir_search):
    hits = [_hit("conversation", "c:1", score=0.9)]

    def fake_ir(query, **kw):
        return hits

    patch_ir_search(fake_ir)
    out = find_op("anything", source="conversation")
    assert out == hits  # raw passthrough, no funnel reshaping


def test_find_drill_false_forwards_all_args(patch_ir_search):
    captured: dict[str, Any] = {}

    def fake_ir(query, **kw):
        captured["query"] = query
        captured.update(kw)
        return []

    patch_ir_search(fake_ir)
    find_op(
        "q", source="docs", scope="x:", top_k=5,
        method="semantic", recency=False,
    )
    assert captured["query"] == "q"
    assert captured["source"] == "docs"
    assert captured["scope"] == "x:"
    assert captured["top_k"] == 5
    assert captured["method"] == "semantic"
    assert captured["recency"] is False


def test_find_drill_false_propagates_error_string(patch_ir_search):
    def fake_ir(query, **kw):
        return "Embedding service unavailable."

    patch_ir_search(fake_ir)
    out = find_op("q", source="conversation")
    assert isinstance(out, str)
    assert "unavailable" in out


# ---------------------------------------------------------------------------
# drill=True, source="summary" — parity with summary_search
# ---------------------------------------------------------------------------


def test_find_drill_true_summary_returns_funnel_shape(patch_ir_search):
    """source='summary' + drill=True should route through the funnel and
    return the same dict shape as `summary_search` does directly."""
    hits = [_hit(
        "summary", "conversation_session:s1:n0",
        namespace="conversation_session", item_id="s1",
        score=0.8, title="root",
    )]

    def fake_ir(query, **kw):
        return hits

    patch_ir_search(fake_ir)
    # Stub out the drill handler so we don't try to load real sessions.
    from work_buddy.summarization import drill_registry

    drill_registry.register_drill_handler(
        "summary",
        lambda ns, iid, q, m, k: {"stub": iid},
    )
    try:
        out = find_op("q", source="summary", drill=True)
    finally:
        drill_registry._reset_for_tests()
        drill_registry._register_defaults()

    assert isinstance(out, dict)
    assert set(out.keys()) >= {
        "query", "scope", "stage1_hits", "candidate_items", "drilled",
    }
    assert len(out["stage1_hits"]) == 1
    assert out["stage1_hits"][0]["item_id"] == "s1"
    assert out["candidate_items"][0]["item_id"] == "s1"
    assert out["drilled"] == {"s1": {"stub": "s1"}}


def test_find_drill_true_source_none_routes_to_summary(patch_ir_search):
    """source=None + drill=True falls through to the summary funnel for
    back-compat with `summary_search` semantics (which has no source
    arg)."""
    def fake_ir(query, **kw):
        # The funnel always sets source="summary" — verify.
        assert kw.get("source") == "summary"
        return []

    patch_ir_search(fake_ir)
    out = find_op("q", drill=True)
    assert isinstance(out, dict)
    assert out["stage1_hits"] == []


# ---------------------------------------------------------------------------
# drill=True, non-summary source
# ---------------------------------------------------------------------------


def test_find_drill_true_unregistered_source_silently_skips_drill(
    patch_ir_search,
):
    """A source without a registered drill handler should produce stage1
    hits + candidate items but an empty `drilled` map. No exception, no
    surprise."""
    hits = [
        _hit("docs", "docs:a", namespace="docs", item_id="docs:a",
             score=0.7, title="doc-a"),
        _hit("docs", "docs:b", namespace="docs", item_id="docs:b",
             score=0.4, title="doc-b"),
    ]

    def fake_ir(query, **kw):
        return hits

    patch_ir_search(fake_ir)
    out = find_op("q", source="docs", drill=True)

    assert isinstance(out, dict)
    assert len(out["stage1_hits"]) == 2
    assert len(out["candidate_items"]) == 2
    assert out["candidate_items"][0]["best_score"] == 0.7  # ranked
    assert out["drilled"] == {}  # no handler -> empty


def test_find_drill_true_with_registered_non_summary_handler(patch_ir_search):
    """A non-summary source with a registered handler should populate
    `drilled`."""
    from work_buddy.summarization import drill_registry

    hits = [
        _hit("chrome", "chrome:tab1", namespace="chrome", item_id="tab1",
             score=0.6, title="Tab 1"),
        _hit("chrome", "chrome:tab2", namespace="chrome", item_id="tab2",
             score=0.5, title="Tab 2"),
    ]

    def fake_ir(query, **kw):
        return hits

    patch_ir_search(fake_ir)

    calls: list[tuple] = []

    def chrome_handler(ns, iid, query, method, top_k):
        calls.append((ns, iid))
        return {"drilled_content": iid}

    drill_registry.register_drill_handler("chrome", chrome_handler)
    try:
        out = find_op("q", source="chrome", drill=True)
    finally:
        drill_registry._reset_for_tests()
        drill_registry._register_defaults()

    assert out["drilled"] == {
        "tab1": {"drilled_content": "tab1"},
        "tab2": {"drilled_content": "tab2"},
    }
    assert set(c[1] for c in calls) == {"tab1", "tab2"}


def test_find_drill_true_error_string_from_ir_returns_error_dict(
    patch_ir_search,
):
    def fake_ir(query, **kw):
        return "Embedding service unavailable."

    patch_ir_search(fake_ir)
    out = find_op("q", source="docs", drill=True)
    assert isinstance(out, dict)
    assert out["stage1_hits"] == []
    assert "unavailable" in out["error"]


def test_find_drill_true_unexpected_ir_return_yields_error(patch_ir_search):
    def fake_ir(query, **kw):
        return {"unexpected": "shape"}

    patch_ir_search(fake_ir)
    out = find_op("q", source="docs", drill=True)
    assert "error" in out
    assert "unexpected" in out["error"]


# ---------------------------------------------------------------------------
# Op registration
# ---------------------------------------------------------------------------


def test_find_op_is_registered():
    """The module-level _register() side effect should have bound the op."""
    from work_buddy.mcp_server.op_registry import get_op

    assert get_op("op.wb.find") is not None
