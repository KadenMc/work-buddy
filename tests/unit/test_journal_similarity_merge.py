"""Tests for `work_buddy.journal_backlog.similarity`.

Covers tag extraction, the embedding-cache plumbing, the merge plan, and
the apply step. The embedding service itself is monkeypatched in tests
so the suite runs without the HTTP service being up.
"""

from __future__ import annotations

import pytest

from work_buddy.journal_backlog import similarity


# ---------------------------------------------------------------------------
# Tag extraction
# ---------------------------------------------------------------------------


class TestExtractInlineTags:
    def test_pulls_wb_namespaced_tags(self):
        tags = similarity.extract_inline_tags(
            "- #wb/TODO research zotero MCP - linked to #paper/ecg-classifier"
        )
        assert "wb/todo" in tags
        assert "paper/ecg-classifier" in tags

    def test_strips_trailing_punctuation(self):
        tags = similarity.extract_inline_tags("ping #wb/TODO, then #urgent.")
        assert "wb/todo" in tags
        assert "urgent" in tags

    def test_dedupes(self):
        tags = similarity.extract_inline_tags("#wb/TODO\n#wb/TODO\n#WB/TODO")
        assert tags.count("wb/todo") == 1

    def test_blank_input(self):
        assert similarity.extract_inline_tags("") == []
        assert similarity.extract_inline_tags(None) == []

    def test_lowercases(self):
        tags = similarity.extract_inline_tags("#Paper/ECG-Classifier")
        assert tags == ["paper/ecg-classifier"]


# ---------------------------------------------------------------------------
# Embedding flow — service mocked
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_embed(monkeypatch):
    """Replace ``embed_for_ir`` with a deterministic stub.

    Returns a dict {text -> hand-picked vector} so individual tests can
    seed similarity scores. Default: every text gets the same vector,
    making cosine similarity 1.0 for every pair (collapses the embedding
    signal to "always merge"; pair similarity then depends on tag +
    proximity).
    """
    vectors_for: dict[str, list[float]] = {}

    def _embed_for_ir(texts, role="document"):
        return [
            vectors_for.get(t, [1.0] + [0.0] * (similarity._EMBED_DIM - 1))
            for t in texts
        ]

    monkeypatch.setattr(
        "work_buddy.embedding.client.embed_for_ir",
        _embed_for_ir,
    )
    return vectors_for


def test_embed_segments_returns_parallel_list(fake_embed, tmp_path, monkeypatch):
    monkeypatch.setattr(similarity, "_cache_path", lambda: tmp_path / "cache")
    out = similarity.embed_segments(["a", "b", "c"])
    assert len(out) == 3
    assert all(v is not None for v in out)
    assert all(len(v) == similarity._EMBED_DIM for v in out)


def test_embed_segments_handles_service_unavailable(monkeypatch):
    """If embed_for_ir returns None, we degrade gracefully — every output
    is None, no exception."""
    monkeypatch.setattr(
        "work_buddy.embedding.client.embed_for_ir",
        lambda texts, role="document": None,
    )
    out = similarity.embed_segments(["a", "b"], cache={})
    assert out == [None, None]


def test_embed_segments_uses_cache(fake_embed, tmp_path, monkeypatch):
    """Cache hits skip the service call; re-embeds only the misses."""
    monkeypatch.setattr(similarity, "_cache_path", lambda: tmp_path / "cache")
    cache = {similarity._content_hash("a"): [9.0] * similarity._EMBED_DIM}

    calls = {"n": 0}

    def _embed_for_ir(texts, role="document"):
        calls["n"] += 1
        return [[7.0] * similarity._EMBED_DIM for _ in texts]

    monkeypatch.setattr(
        "work_buddy.embedding.client.embed_for_ir",
        _embed_for_ir,
    )

    out = similarity.embed_segments(["a", "b"], cache=cache)
    # ``a`` came from cache (vector 9.0…); ``b`` was embedded fresh (7.0…).
    assert out[0][0] == 9.0
    assert out[1][0] == 7.0
    # Service called once for the single miss.
    assert calls["n"] == 1
    # Cache mutated to include the miss.
    assert similarity._content_hash("b") in cache


_HAS_NUMPY = True
try:
    import numpy as _np  # noqa: F401
except ImportError:
    _HAS_NUMPY = False


@pytest.mark.skipif(not _HAS_NUMPY, reason="numpy not installed; cache is a no-op")
def test_cache_round_trip(tmp_path, monkeypatch):
    """Saved cache should load back identically when model_key/version match.
    Skipped when numpy isn't installed — the cache is purely a perf
    optimisation that no-ops in numpy-less environments (mirrors
    ``knowledge/persistence.py``'s pattern)."""
    monkeypatch.setattr(similarity, "_cache_path", lambda: tmp_path / "cache")
    cache = {
        "h1": [0.1] * similarity._EMBED_DIM,
        "h2": [0.2] * similarity._EMBED_DIM,
    }
    similarity._save_cache(cache)
    loaded = similarity._load_cache()
    assert set(loaded.keys()) == {"h1", "h2"}
    # float16 round-trip introduces small precision loss — check approx.
    assert abs(loaded["h1"][0] - 0.1) < 0.001
    assert abs(loaded["h2"][0] - 0.2) < 0.001


@pytest.mark.skipif(not _HAS_NUMPY, reason="numpy not installed; cache is a no-op")
def test_cache_version_bump_invalidates(tmp_path, monkeypatch):
    monkeypatch.setattr(similarity, "_cache_path", lambda: tmp_path / "cache")
    similarity._save_cache({"h": [0.0] * similarity._EMBED_DIM})
    monkeypatch.setattr(similarity, "CACHE_VERSION", similarity.CACHE_VERSION + 1)
    assert similarity._load_cache() == {}


def test_cache_load_returns_empty_when_no_numpy(monkeypatch, tmp_path):
    """Even without numpy, _load_cache returns {} (graceful no-op)."""
    monkeypatch.setattr(similarity, "_cache_path", lambda: tmp_path / "cache")
    assert similarity._load_cache() == {}


def test_cache_save_swallows_no_numpy(monkeypatch, tmp_path):
    """_save_cache must never raise in numpy-less environments."""
    monkeypatch.setattr(similarity, "_cache_path", lambda: tmp_path / "cache")
    similarity._save_cache({"h": [0.0] * similarity._EMBED_DIM})  # no-op when no numpy


# ---------------------------------------------------------------------------
# Merge planning
# ---------------------------------------------------------------------------


def _seg(sid: str, text: str, line_count: int = 1) -> dict:
    return {
        "id": sid,
        "raw_text": text,
        "line_count": line_count,
        "source_dates": [],
        "has_multi_flag": False,
    }


def test_plan_merges_empty_input():
    plan = similarity.plan_merges([], use_cache=False)
    assert plan["merges"] == []
    assert plan["pair_count"] == 0


def test_plan_merges_single_segment_returns_no_pairs():
    plan = similarity.plan_merges(
        [_seg("t_1", "lonely")], use_cache=False,
    )
    assert plan["merges"] == []
    assert plan["pair_count"] == 0


def test_plan_merges_high_similarity_emits_merge(fake_embed, tmp_path, monkeypatch):
    """When the embedding signal makes two segments look identical AND they
    share tags AND they're adjacent, the fused score crosses threshold."""
    monkeypatch.setattr(similarity, "_cache_path", lambda: tmp_path / "cache")
    segments = [
        _seg("t_1", "#wb/TODO research zotero plugin"),
        _seg("t_2", "  - [zotero-mcp on github](url) #wb/TODO"),
        _seg("t_3", "completely unrelated text about haircuts"),
    ]
    plan = similarity.plan_merges(segments, use_cache=False)
    assert plan["embed_status"] == "ok"
    # Fixture vectors are identical for every text → cosine 1.0 for all
    # pairs. With shared tag #wb/todo and proximity i=0,j=1 the first
    # pair fuses well above 0.55. The unrelated segment t_3 has no
    # shared tag with t_1 / t_2, lower proximity, but high embedding —
    # depending on weights it may or may not merge. Just check t_1+t_2.
    merged_pairs = [tuple(sorted(m["ids"])) for m in plan["merges"]]
    assert ("t_1", "t_2") in merged_pairs


def test_plan_merges_falls_back_to_tag_proximity_on_embedding_failure(monkeypatch, tmp_path):
    """When the embedding service is unavailable, merges should still work
    if tag + proximity alone clear the threshold."""
    monkeypatch.setattr(similarity, "_cache_path", lambda: tmp_path / "cache")
    monkeypatch.setattr(
        "work_buddy.embedding.client.embed_for_ir",
        lambda texts, role="document": None,
    )
    # Two segments sharing a tag, adjacent in order. With weights
    # 0.55/0.35/0.10, embedding=0 contributes 0; tag match gives 0.35;
    # proximity at i=0,j=1 with sigma=0.2 gives ~0.998 → 0.10 contrib.
    # Fused ~ 0.45. Below default threshold 0.55, so default doesn't
    # merge. But with a lower threshold it will.
    segments = [
        _seg("t_1", "#wb/TODO foo"),
        _seg("t_2", "#wb/TODO foo continued"),
    ]
    plan_default = similarity.plan_merges(segments, use_cache=False)
    assert plan_default["embed_status"] == "service_unavailable"
    # Default threshold (0.55) — no merge from tag+prox alone.
    assert plan_default["merges"] == []
    # Lower threshold — merge fires.
    plan_low = similarity.plan_merges(segments, use_cache=False, threshold=0.3)
    assert len(plan_low["merges"]) == 1


def test_plan_merges_records_partial_status(monkeypatch, tmp_path):
    """Some segments embed, others don't (e.g. mid-batch service hiccup) —
    embed_status should be 'partial'."""
    monkeypatch.setattr(similarity, "_cache_path", lambda: tmp_path / "cache")

    # Stub embed_segments to return one vector + one None (simulates a
    # caching-then-failed-batch situation; we test the helper's branch).
    real_embed = similarity.embed_segments

    def _half_failing(texts, *, cache=None):
        out = real_embed(texts, cache=cache or {})
        if out:
            out[0] = None
        return out

    # Make fresh embeddings work, then have our shim drop the first one.
    monkeypatch.setattr(
        "work_buddy.embedding.client.embed_for_ir",
        lambda texts, role="document": [
            [1.0] + [0.0] * (similarity._EMBED_DIM - 1) for _ in texts
        ],
    )
    monkeypatch.setattr(similarity, "embed_segments", _half_failing)

    plan = similarity.plan_merges(
        [_seg("t_1", "a"), _seg("t_2", "b")], use_cache=False,
    )
    assert plan["embed_status"] == "partial"
    assert plan["embedded"] == 1
    assert plan["skipped"] == 1


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_merges_concatenates_raw_text():
    segments = [
        _seg("t_1", "first line"),
        _seg("t_2", "  - second line"),
        _seg("t_3", "unrelated"),
    ]
    merges = [{"ids": ["t_1", "t_2"], "fused_score": 0.7}]
    out = similarity.apply_merges(segments, merges)
    assert len(out) == 2
    assert out[0]["id"] == "t_1"
    assert out[0]["raw_text"] == "first line\n  - second line"
    assert out[0]["merged_from"] == ["t_1", "t_2"]
    assert out[0]["merge_score"] == 0.7
    assert out[1]["id"] == "t_3"


def test_apply_merges_sums_line_counts():
    segments = [
        _seg("t_1", "x", line_count=2),
        _seg("t_2", "y", line_count=3),
    ]
    out = similarity.apply_merges(
        segments,
        [{"ids": ["t_1", "t_2"], "fused_score": 0.7}],
    )
    assert out[0]["line_count"] == 5


def test_apply_merges_preserves_first_member_id_for_cleanup_compat():
    """The cleanup adapter keys off the inciting line_text, which derives
    from segment id at spawn time. Keeping the first member's id means a
    merged sub-thread's cleanup still resolves against the same source
    line as before, so the cleanup adapter needs no changes."""
    segments = [_seg("t_first", "a"), _seg("t_second", "b")]
    out = similarity.apply_merges(
        segments,
        [{"ids": ["t_first", "t_second"], "fused_score": 0.7}],
    )
    assert out[0]["id"] == "t_first"


def test_apply_merges_no_op_when_plan_empty():
    segments = [_seg("t_1", "a"), _seg("t_2", "b")]
    out = similarity.apply_merges(segments, [])
    assert out == segments


def test_apply_merges_unions_source_dates():
    segments = [
        {**_seg("t_1", "a"), "source_dates": ["2026-04-30"]},
        {**_seg("t_2", "b"), "source_dates": ["2026-05-01"]},
    ]
    out = similarity.apply_merges(
        segments,
        [{"ids": ["t_1", "t_2"], "fused_score": 0.7}],
    )
    assert out[0]["source_dates"] == ["2026-04-30", "2026-05-01"]


# ---------------------------------------------------------------------------
# End-to-end convenience
# ---------------------------------------------------------------------------


def test_merge_segments_returns_meta_with_expected_fields(fake_embed, tmp_path, monkeypatch):
    monkeypatch.setattr(similarity, "_cache_path", lambda: tmp_path / "cache")
    segments = [
        _seg("t_1", "#wb/TODO topic"),
        _seg("t_2", "  - bullet under topic #wb/TODO"),
    ]
    merged, meta = similarity.merge_segments(segments)
    assert meta["before_count"] == 2
    assert meta["after_count"] == len(merged)
    assert "applied_merges" in meta
    assert "embed_status" in meta
