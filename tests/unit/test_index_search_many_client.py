"""Tests for embedding/client.py::index_search_many — the batched consolidated-index
search wrapper used by the dev-document scan. _request is monkeypatched (no service)."""

from __future__ import annotations

from work_buddy.embedding import client


def test_returns_per_query_results(monkeypatch):
    captured = {}

    def _fake_request(method, path, payload, timeout=None):
        captured.update(method=method, path=path, payload=payload, timeout=timeout)
        return {"results": [[{"doc_id": "knowledge:x", "score": 1.0}], []]}

    monkeypatch.setattr(client, "_request", _fake_request)
    out = client.index_search_many(
        ["q1", "q2"], top_k=8, partitions=["knowledge"],
        filters={"scope": "system"}, timeout_s=25,
    )
    assert out == [[{"doc_id": "knowledge:x", "score": 1.0}], []]  # one list per query
    assert captured["method"] == "POST" and captured["path"] == "/index/search_many"
    assert captured["timeout"] == 25
    assert captured["payload"]["queries"] == ["q1", "q2"]
    assert captured["payload"]["partitions"] == ["knowledge"]
    assert captured["payload"]["filters"] == {"scope": "system"}


def test_none_when_service_unreachable(monkeypatch):
    monkeypatch.setattr(client, "_request", lambda *a, **k: None)
    assert client.index_search_many(["q"]) is None


def test_omits_optional_keys_when_unset(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client, "_request",
        lambda method, path, payload, timeout=None: captured.update(payload=payload)
        or {"results": []},
    )
    client.index_search_many(["q"])  # no partitions/filters/scope/rrf_k
    p = captured["payload"]
    assert "partitions" not in p and "filters" not in p and "scope" not in p
    assert p["queries"] == ["q"]
