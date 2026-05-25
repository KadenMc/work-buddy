"""Tests for `work_buddy.summarization.funnel.summary_search`.

Pure orchestration over `ir.search.search` + a per-namespace drill handler.
Injects both via parameters so tests don't touch the real IR engine or
session inspector. Verifies: stage-1 hit shape, candidate-item aggregation,
ranking, scope wiring, drill dispatch, drill=False short-circuit, error
propagation.
"""

from __future__ import annotations

from typing import Any

import pytest

from work_buddy.summarization.funnel import summary_search


def _hit(
    namespace: str,
    item_id: str,
    *,
    level: int = 1,
    ordinal: int = 0,
    title: str = "T",
    summary: str = "S",
    score: float = 0.5,
    source_ref: Any = None,
) -> dict[str, Any]:
    """Build a fake ir.search.search result row."""
    return {
        "doc_id": f"{namespace}:{item_id}:n{ordinal}",
        "score": score,
        "source": "summary",
        "display_text": summary,
        "metadata": {
            "namespace": namespace,
            "item_id": item_id,
            "level": level,
            "ordinal": ordinal,
            "source_ref": source_ref,
            "generated_at": "2026-05-25T00:00:00Z",
            "model": "claude-haiku-4-5",
            "extra": {"title": title, "topic_index": ordinal},
        },
    }


# ---------------------------------------------------------------------------
# stage 1
# ---------------------------------------------------------------------------


def test_stage1_hits_carry_normalized_fields():
    fake_hits = [
        _hit("conversation_session", "sess-a",
             title="Topic A", summary="About A", score=0.9,
             source_ref={"span_start": 0, "span_end": 5}),
    ]

    def fake_ir(query, **kw):
        return fake_hits

    out = summary_search(
        "anything", drill=False, ir_search_fn=fake_ir,
    )

    assert len(out["stage1_hits"]) == 1
    h = out["stage1_hits"][0]
    assert h["namespace"] == "conversation_session"
    assert h["item_id"] == "sess-a"
    assert h["title"] == "Topic A"
    assert h["summary"] == "About A"
    assert h["score"] == 0.9
    assert h["source_ref"] == {"span_start": 0, "span_end": 5}


def test_stage1_passes_scope_when_namespace_supplied():
    captured: dict[str, Any] = {}

    def fake_ir(query, **kw):
        captured.update(kw)
        return []

    summary_search(
        "q",
        namespace="conversation_session",
        drill=False,
        ir_search_fn=fake_ir,
    )

    # `scope` is the namespace plus a colon (doc_id prefix-match).
    assert captured["scope"] == "conversation_session:"
    assert captured["source"] == "summary"


def test_stage1_passes_no_scope_when_namespace_omitted():
    captured: dict[str, Any] = {}

    def fake_ir(query, **kw):
        captured.update(kw)
        return []

    summary_search("q", drill=False, ir_search_fn=fake_ir)
    assert captured["scope"] is None


def test_stage1_drops_hits_missing_item_id():
    bad = [
        {
            "doc_id": "x:y:n0",
            "score": 0.5,
            "metadata": {"namespace": "x"},  # no item_id
            "display_text": "skip me",
        },
    ]

    def fake_ir(query, **kw):
        return bad

    out = summary_search("q", drill=False, ir_search_fn=fake_ir)
    assert out["stage1_hits"] == []


# ---------------------------------------------------------------------------
# candidate aggregation
# ---------------------------------------------------------------------------


def test_candidate_items_aggregate_by_item_id():
    fake_hits = [
        _hit("conversation_session", "sess-a",
             ordinal=0, title="T0", score=0.4),
        _hit("conversation_session", "sess-a",
             ordinal=1, title="T1", score=0.7),
        _hit("conversation_session", "sess-a",
             ordinal=2, title="T2", score=0.5),
        _hit("conversation_session", "sess-b",
             ordinal=0, title="B", score=0.6),
    ]

    def fake_ir(query, **kw):
        return fake_hits

    out = summary_search("q", drill=False, ir_search_fn=fake_ir)
    cands = out["candidate_items"]

    assert len(cands) == 2
    # Ranked by best_score: sess-a (0.7) above sess-b (0.6).
    assert cands[0]["item_id"] == "sess-a"
    assert cands[0]["best_score"] == 0.7
    assert cands[0]["n_hits"] == 3
    assert cands[1]["item_id"] == "sess-b"
    assert cands[1]["best_score"] == 0.6
    assert cands[1]["n_hits"] == 1


def test_candidate_top_titles_dedup_and_cap_at_three():
    fake_hits = [
        _hit("ns", "id", ordinal=i, title=t, score=0.5)
        for i, t in enumerate(["A", "B", "A", "C", "D"])
    ]

    def fake_ir(query, **kw):
        return fake_hits

    out = summary_search("q", drill=False, ir_search_fn=fake_ir)
    cand = out["candidate_items"][0]
    # First three distinct titles encountered.
    assert cand["top_titles"] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# drill
# ---------------------------------------------------------------------------


def test_drill_dispatches_per_top_item():
    fake_hits = [
        _hit("conversation_session", f"sess-{i}", score=1.0 - 0.1 * i)
        for i in range(5)
    ]
    drilled_calls: list[tuple[str, str]] = []

    def fake_ir(query, **kw):
        return fake_hits

    def fake_drill(ns, iid, query, method, top_k):
        drilled_calls.append((ns, iid))
        return {"hits": [f"hit-for-{iid}"]}

    out = summary_search(
        "q",
        drill=True,
        drill_top_k=3,
        ir_search_fn=fake_ir,
        drill_handler=fake_drill,
    )

    # Top 3 items drilled, in best-score order.
    assert [iid for _, iid in drilled_calls] == ["sess-0", "sess-1", "sess-2"]
    assert set(out["drilled"].keys()) == {"sess-0", "sess-1", "sess-2"}
    assert out["drilled"]["sess-0"] == {"hits": ["hit-for-sess-0"]}


def test_drill_false_short_circuits():
    fake_hits = [_hit("ns", "id-a")]
    drilled_calls: list[Any] = []

    def fake_ir(query, **kw):
        return fake_hits

    def fake_drill(*args, **kw):
        drilled_calls.append(args)
        return "should not happen"

    out = summary_search(
        "q",
        drill=False,
        ir_search_fn=fake_ir,
        drill_handler=fake_drill,
    )

    assert out["drilled"] == {}
    assert drilled_calls == []


def test_drill_handler_can_return_none_for_unsupported_namespace():
    """A namespace without a drill handler returns None per item — funnel
    still surfaces the coarse hits."""
    fake_hits = [_hit("chrome_page", "page-a", score=0.8)]

    def fake_ir(query, **kw):
        return fake_hits

    out = summary_search(
        "q",
        drill=True,
        ir_search_fn=fake_ir,
        # Use the default handler which only knows conversation_session.
    )

    assert "page-a" in out["drilled"]
    assert out["drilled"]["page-a"] is None


# ---------------------------------------------------------------------------
# error propagation
# ---------------------------------------------------------------------------


def test_error_string_from_ir_propagates():
    def fake_ir(query, **kw):
        return "Embedding service unavailable."

    out = summary_search("q", drill=False, ir_search_fn=fake_ir)
    assert out["stage1_hits"] == []
    assert out["candidate_items"] == []
    assert "unavailable" in out["error"]


def test_drill_skipped_when_stage1_errored():
    def fake_ir(query, **kw):
        return "Embedding service unavailable."

    drill_called = {"n": 0}

    def fake_drill(*a, **kw):
        drill_called["n"] += 1
        return None

    out = summary_search(
        "q", drill=True, ir_search_fn=fake_ir, drill_handler=fake_drill,
    )
    assert drill_called["n"] == 0
    assert out["drilled"] == {}


def test_unexpected_ir_return_type_yields_error():
    def fake_ir(query, **kw):
        return {"unexpected": "shape"}

    out = summary_search("q", drill=False, ir_search_fn=fake_ir)
    assert out["error"]
    assert "unexpected" in out["error"]


# ---------------------------------------------------------------------------
# default drill handler
# ---------------------------------------------------------------------------


def test_default_drill_handler_dispatches_to_session_search(monkeypatch):
    """The default handler routes conversation_session to session_search."""
    from work_buddy.summarization import funnel as funnel_mod
    from work_buddy.sessions import inspector

    calls: list[dict[str, Any]] = []

    def fake_session_search(
        session_id, query, method="keyword,semantic", top_k=5,
    ):
        calls.append({
            "session_id": session_id, "query": query,
            "method": method, "top_k": top_k,
        })
        return {"session_id": session_id, "hits": []}

    monkeypatch.setattr(inspector, "session_search", fake_session_search)

    result = funnel_mod._default_drill_handler(
        "conversation_session", "sess-a", "the query",
        "keyword,semantic", 7,
    )
    assert calls == [{
        "session_id": "sess-a", "query": "the query",
        "method": "keyword,semantic", "top_k": 7,
    }]
    assert result == {"session_id": "sess-a", "hits": []}


def test_default_drill_handler_returns_none_for_unknown_namespace():
    from work_buddy.summarization import funnel as funnel_mod

    assert funnel_mod._default_drill_handler(
        "unknown_namespace", "id", "q", "keyword", 5,
    ) is None
