"""Unit tests for the namespace_lookup + hybrid/fallback paths (Phase 7)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from work_buddy.obsidian.tasks import store
from work_buddy.obsidian.tasks import namespace_suggest as ns_mod
from work_buddy.obsidian.tasks.namespace_suggest import (
    _build_candidates,
    _tag_to_candidate_texts,
    namespace_lookup,
    task_namespace_suggest,
)


@pytest.fixture
def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "tasks.sqlite")


def _seed(ids_with_tags):
    for tid, tags in ids_with_tags.items():
        store.create(task_id=tid, state="inbox")
        store.set_task_tags(tid, tags)


# ── Candidate construction ──────────────────────────────────────


class TestTagToCandidateTexts:
    def test_single_segment(self):
        texts = _tag_to_candidate_texts("admin")
        assert "admin" in texts

    def test_nested_expands_segments(self):
        texts = _tag_to_candidate_texts("paper/ecg-classifier")
        # Full path joined with spaces
        assert "paper ecg classifier" in texts
        # Individual segments hyphen-split
        assert "ecg classifier" in texts

    def test_build_candidates_shape(self):
        universe = [{"tag": "paper/ecg", "count": 2, "recent_count": 1}]
        c = _build_candidates(universe)
        assert len(c) == 1
        assert c[0]["name"] == "paper/ecg"
        assert isinstance(c[0]["texts"], list) and len(c[0]["texts"]) >= 1


# ── task_namespace_suggest: fallback path (embedding service down) ──


class TestFallbackPath:
    def test_falls_back_to_tokens_when_service_down(self, _isolated_store, monkeypatch):
        _seed({
            "t-a": [("paper/ecg-classifier", True)],
            "t-b": [("paper/ecg-classifier", True)],
        })
        monkeypatch.setattr(ns_mod.embedding_client, "is_available", lambda: False)
        out = task_namespace_suggest("draft ECG augmentation writeup")
        assert out["service_used"] == "tokens"
        tags = [s["tag"] for s in out["suggestions"]]
        assert "paper/ecg-classifier" in tags
        for s in out["suggestions"]:
            assert s["method"] == "tokens"
            assert s["exists"] is True

    def test_fallback_empty_on_no_overlap(self, _isolated_store, monkeypatch):
        _seed({
            "t-a": [("paper/ecg", True)],
            "t-b": [("paper/ecg", True)],
        })
        monkeypatch.setattr(ns_mod.embedding_client, "is_available", lambda: False)
        # Query that shares no tokens with any tag → no suggestions
        out = task_namespace_suggest("grocery shopping run")
        assert out["service_used"] == "none"
        assert out["suggestions"] == []


# ── task_namespace_suggest: hybrid path ──


class TestHybridPath:
    def test_hybrid_results_returned_when_service_up(self, _isolated_store, monkeypatch):
        _seed({
            "t-a": [("paper/ecg-classifier", True)],
            "t-b": [("paper/ecg-classifier", True)],
            "t-c": [("admin/taxes", True)],
            "t-d": [("admin/taxes", True)],
        })

        monkeypatch.setattr(ns_mod.embedding_client, "is_available", lambda: True)
        # Fake hybrid_search — the scorer is the service, not us.
        def _fake(query, candidates, **_kw):
            # Pretend it ranked paper/ecg-classifier first.
            return [
                {"name": "paper/ecg-classifier", "score": 0.92,
                 "bm25_score": 0.5, "embed_score": 0.97},
                {"name": "admin/taxes", "score": 0.11,
                 "bm25_score": 0.05, "embed_score": 0.15},
            ]
        monkeypatch.setattr(ns_mod.embedding_client, "hybrid_search", _fake)

        out = task_namespace_suggest("prepare ECG augmentation experiment", limit=2)
        assert out["service_used"] == "hybrid"
        assert len(out["suggestions"]) == 2
        top = out["suggestions"][0]
        assert top["tag"] == "paper/ecg-classifier"
        assert top["method"] == "hybrid"
        assert top["exists"] is True
        # Real count passed through from universe, not from the fake scorer.
        assert top["count"] == 2

    def test_hybrid_empty_drops_to_tokens(self, _isolated_store, monkeypatch):
        _seed({
            "t-a": [("paper/ecg-classifier", True)],
            "t-b": [("paper/ecg-classifier", True)],
        })
        monkeypatch.setattr(ns_mod.embedding_client, "is_available", lambda: True)
        monkeypatch.setattr(ns_mod.embedding_client, "hybrid_search", lambda *a, **k: [])
        out = task_namespace_suggest("ECG paper writeup")
        # Hybrid returned nothing → we drop to tokens, which finds a hit.
        assert out["service_used"] == "tokens"
        assert any(s["tag"] == "paper/ecg-classifier" for s in out["suggestions"])


# ── namespace_lookup ───────────────────────────────────────────


class TestNamespaceLookup:
    def test_empty_query(self, _isolated_store):
        out = namespace_lookup("")
        assert out["exact_match"] is False
        assert out["matches"] == []
        assert out["universe_size"] == 0

    def test_exact_match_flag(self, _isolated_store, monkeypatch):
        _seed({
            "t-a": [("paper/ecg-classifier", True)],
            "t-b": [("paper/ecg-classifier", True)],
        })
        monkeypatch.setattr(ns_mod.embedding_client, "is_available", lambda: False)
        out = namespace_lookup("paper/ecg-classifier")
        assert out["exact_match"] is True

    def test_exact_match_strips_hash(self, _isolated_store, monkeypatch):
        _seed({
            "t-a": [("admin", True)],
            "t-b": [("admin", True)],
        })
        monkeypatch.setattr(ns_mod.embedding_client, "is_available", lambda: False)
        out = namespace_lookup("#admin")
        assert out["query"] == "admin"
        assert out["exact_match"] is True

    def test_near_match_returns_candidate(self, _isolated_store, monkeypatch):
        _seed({
            "t-a": [("health/sleep", True)],
            "t-b": [("health/sleep", True)],
        })
        monkeypatch.setattr(ns_mod.embedding_client, "is_available", lambda: False)
        # User is about to mint "wellness/sleep" — we should find health/sleep.
        out = namespace_lookup("wellness/sleep")
        assert out["exact_match"] is False
        tags = [m["tag"] for m in out["matches"]]
        assert "health/sleep" in tags
