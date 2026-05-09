"""Unit tests for ``rrf_combine``: outer-layer fusion of multiple ``search()``
result lists.

The hybrid index already does RRF internally over BM25 + dense rankings.
``rrf_combine`` is the *outer* layer — for callers that run multiple
independent ``search()`` calls (different queries, different signal sources)
and want the same equal-voice rank fusion across those.

The classic RRF property under test: a document that appears in *multiple*
rankings outranks one that appears in only one, even when the score in any
single ranking is similar. That's why ``rrf_combine`` exists instead of just
concatenating the queries — concatenation dilutes the structural signal
under longer prosier queries.
"""

from __future__ import annotations

from work_buddy.knowledge.search import rrf_combine


def test_rrf_combine_empty_input():
    """Empty input (in any flavor) yields an empty list."""
    assert rrf_combine([]) == []
    assert rrf_combine([[]]) == []
    assert rrf_combine([[], []]) == []


def test_rrf_combine_single_ranking_is_idempotent():
    """One ranking in, same paths in same order out, with rrf_score populated."""
    ranking = [
        {"path": "a", "name": "A", "score": 0.9},
        {"path": "b", "name": "B", "score": 0.5},
        {"path": "c", "name": "C", "score": 0.1},
    ]
    out = rrf_combine([ranking])
    assert [hit["path"] for hit in out] == ["a", "b", "c"]
    # rrf_score = 1/(k + rank+1) for default k=60
    assert out[0]["rrf_score"] == 1.0 / 61
    assert out[1]["rrf_score"] == 1.0 / 62
    assert out[2]["rrf_score"] == 1.0 / 63
    # Original metadata preserved
    assert out[0]["name"] == "A"


def test_rrf_combine_documents_in_multiple_rankings_rank_higher():
    """A document appearing in both rankings outranks docs appearing in only one.

    This is the property that makes RRF the right tool for fusing
    independent signal sources — a hit ranked moderately by *both* signals
    should beat a hit ranked highly by *one*.
    """
    ranking_paths = [{"path": "shared", "score": 0.5}, {"path": "only_paths", "score": 0.4}]
    ranking_docs = [{"path": "shared", "score": 0.5}, {"path": "only_docs", "score": 0.4}]

    out = rrf_combine([ranking_paths, ranking_docs])
    paths = [hit["path"] for hit in out]
    # `shared` appears in both → 1/61 + 1/61 = 2/61 ≈ 0.0328
    # `only_paths` and `only_docs` each appear in one → 1/62 ≈ 0.0161
    assert paths[0] == "shared", f"shared should rank first; got {paths}"
    # The single-ranking hits should follow, in either order
    assert set(paths[1:]) == {"only_paths", "only_docs"}
    # And `shared`'s RRF score should beat the others'
    assert out[0]["rrf_score"] > out[1]["rrf_score"]


def test_rrf_combine_documents_in_only_one_ranking_still_appear():
    """RRF doesn't drop singletons — every document with any rank shows up.

    Important for the dev-document scan: a unit that surfaces only via the
    structural query (e.g., a canonical entry-point match with no domain
    overlap) still needs to appear in the final list.
    """
    ranking_a = [{"path": "a", "score": 0.9}]
    ranking_b = [{"path": "b", "score": 0.9}]
    out = rrf_combine([ranking_a, ranking_b])
    paths = {hit["path"] for hit in out}
    assert paths == {"a", "b"}


def test_rrf_combine_first_metadata_wins_on_collisions():
    """When the same path appears in multiple rankings, the first occurrence's
    metadata is preserved — later occurrences contribute only to the score.

    This matters for callers that synthesize a ``why`` field outside the
    fusion: they need to know which ranking each path appeared in. The
    deterministic "first ranking wins for metadata" rule keeps that
    bookkeeping predictable.
    """
    r1 = [{"path": "x", "name": "X-from-ranking-1", "score": 0.5}]
    r2 = [{"path": "x", "name": "X-from-ranking-2", "score": 0.5}]
    out = rrf_combine([r1, r2])
    assert out[0]["name"] == "X-from-ranking-1"


def test_rrf_combine_does_not_mutate_inputs():
    """Inputs are not modified; output dicts are shallow copies."""
    ranking = [{"path": "a", "score": 0.9}]
    snapshot = list(ranking[0].items())
    out = rrf_combine([ranking])
    # Output has rrf_score added; input does not.
    assert "rrf_score" in out[0]
    assert "rrf_score" not in ranking[0]
    assert list(ranking[0].items()) == snapshot


def test_rrf_combine_skips_hits_without_path():
    """Malformed result entries (no ``path``) are silently dropped, not crashed."""
    out = rrf_combine([[{"score": 0.9}, {"path": "ok", "score": 0.5}]])
    paths = [hit["path"] for hit in out]
    assert paths == ["ok"]


def test_rrf_combine_custom_k_changes_rank_compression():
    """Higher k flattens rank differences; lower k sharpens them.

    Sanity check: with very small k, the gap between ranks 1 and 2 is
    proportionally larger than with the default k=60.
    """
    ranking = [{"path": "a", "score": 0.9}, {"path": "b", "score": 0.5}]
    sharp = rrf_combine([ranking], k=1)
    flat = rrf_combine([ranking], k=1000)
    sharp_ratio = sharp[0]["rrf_score"] / sharp[1]["rrf_score"]
    flat_ratio = flat[0]["rrf_score"] / flat[1]["rrf_score"]
    assert sharp_ratio > flat_ratio
