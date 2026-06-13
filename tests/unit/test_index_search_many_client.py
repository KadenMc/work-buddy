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


# --- index_search (single-query sibling) ---

def test_index_search_returns_single_list(monkeypatch):
    captured = {}

    def _req(method, path, payload, timeout=None):
        captured.update(method=method, path=path, payload=payload, timeout=timeout)
        return {"results": [{"doc_id": "knowledge:x", "score": 1.0}]}

    monkeypatch.setattr(client, "_request", _req)
    out = client.index_search(
        "q", top_k=8, partitions=["knowledge"], filters={"scope": "system"}, timeout_s=15,
    )
    assert out == [{"doc_id": "knowledge:x", "score": 1.0}]  # one flat list, not list-of-lists
    assert captured["method"] == "POST" and captured["path"] == "/index/search"
    assert captured["timeout"] == 15
    assert captured["payload"]["query"] == "q"
    assert captured["payload"]["partitions"] == ["knowledge"]
    assert captured["payload"]["filters"] == {"scope": "system"}


def test_index_search_none_when_unreachable(monkeypatch):
    monkeypatch.setattr(client, "_request", lambda *a, **k: None)
    assert client.index_search("q") is None


# --- warm-retry on the explicit "warming" signal ---

def _seq_request(monkeypatch, responses):
    """Monkeypatch _request to return queued responses in order, recording each call."""
    calls: list[dict] = []

    def _req(method, path, payload, timeout=None):
        calls.append({"path": path, "payload": payload, "timeout": timeout})
        return responses[len(calls) - 1]

    monkeypatch.setattr(client, "_request", _req)
    monkeypatch.setattr(client.time, "sleep", lambda *_a, **_k: None)  # no real wait
    return calls


def test_warm_retry_waits_then_retries_blocking(monkeypatch):
    warm_hits = [{"doc_id": "knowledge:x", "score": 1.0}]
    calls = _seq_request(monkeypatch, [
        {"results": [], "warming": ["knowledge"], "retry_after_s": 3},  # cold
        {"results": warm_hits},                                          # warm retry
    ])
    out = client.index_search("q", partitions=["knowledge"], warm_retry=True)
    assert out == warm_hits                       # the warm result supersedes the cold one
    assert len(calls) == 2                         # exactly one retry
    assert "block_until_warm" not in calls[0]["payload"]
    assert calls[1]["payload"]["block_until_warm"] is True  # retry blocks until warm
    assert calls[1]["timeout"] >= client._COLD_LOAD_TIMEOUT_S  # extended budget


def test_no_retry_when_warm_retry_disabled(monkeypatch):
    calls = _seq_request(monkeypatch, [
        {"results": [], "warming": ["knowledge"], "retry_after_s": 3},
    ])
    out = client.index_search("q", warm_retry=False)  # opted out
    assert out == []
    assert len(calls) == 1                            # no retry — caller falls back itself


def test_none_is_down_not_warming_so_not_retried(monkeypatch):
    # None == service down (distinct from warming); must NOT trigger a warm-retry.
    calls = _seq_request(monkeypatch, [None])
    assert client.index_search("q", warm_retry=True) is None
    assert len(calls) == 1


def test_no_warming_field_returns_results_without_retry(monkeypatch):
    hits = [{"doc_id": "knowledge:x", "score": 1.0}]
    calls = _seq_request(monkeypatch, [{"results": hits}])  # already warm
    assert client.index_search("q", warm_retry=True) == hits
    assert len(calls) == 1


def test_search_many_warm_retry(monkeypatch):
    warm = [[{"doc_id": "knowledge:x", "score": 1.0}], []]
    calls = _seq_request(monkeypatch, [
        {"results": [[], []], "warming": ["knowledge"]},  # cold (no retry_after_s → default wait)
        {"results": warm},                                 # warm retry
    ])
    out = client.index_search_many(["q1", "q2"], partitions=["knowledge"], warm_retry=True)
    assert out == warm
    assert len(calls) == 2
    assert calls[1]["payload"]["block_until_warm"] is True
