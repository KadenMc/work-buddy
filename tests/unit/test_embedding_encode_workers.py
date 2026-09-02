"""Persistent-worker regression coverage for the embedding HTTP service.

Werkzeug's development server creates a new Python thread for every request.
Calling SentenceTransformer from those transient threads caused each request
to leave a native OpenMP team resident. These tests use Python ``Thread``
objects as the observable boundary, avoiding fragile process-wide OS thread
counts while pinning the execution model that prevents the leak.
"""
from __future__ import annotations

import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np


def test_worker_count_matches_local_broker_capacity() -> None:
    from work_buddy.embedding.service import _configured_encode_workers

    assert _configured_encode_workers(None) == 1
    assert _configured_encode_workers({"inference": {"profiles": {}}}) == 1
    assert _configured_encode_workers({
        "inference": {
            "profiles": {"local:embedding": {"max_concurrent": 3}},
        },
    }) == 3
    assert _configured_encode_workers({
        "inference": {
            "profiles": {"local:embedding": {"max_concurrent": "invalid"}},
        },
    }) == 1
    assert _configured_encode_workers({
        "inference": {"profiles": {"local:embedding": "invalid"}},
    }) == 1


def test_embed_endpoint_reuses_worker_across_transient_callers(
    monkeypatch,
) -> None:
    """Sequential /embed threads must enter native encode on one stable worker.

    The old direct ``model.encode(...)`` call records the two transient caller
    objects here and fails. A persistent executor records the same worker
    object twice, irrespective of whether the OS later recycles thread IDs.
    """
    import work_buddy.embedding.service as service
    from work_buddy.inference import local_slot

    worker_threads: list[threading.Thread] = []
    admission_threads: list[threading.Thread] = []
    results: list[dict] = []

    class _Model:
        def encode(self, texts, **_kwargs):
            worker_threads.append(threading.current_thread())
            return np.ones((len(texts), 2), dtype=np.float32)

    @contextlib.contextmanager
    def _admitted(_priority):
        admission_threads.append(threading.current_thread())
        yield None

    executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="test-embedding-encode",
    )
    monkeypatch.setattr(local_slot, "local_embed_slot", _admitted)
    monkeypatch.setattr(service, "_encode_executor", executor, raising=False)
    monkeypatch.setattr(service, "_get_model", lambda _key: _Model())

    def _request() -> None:
        with service.app.test_client() as client:
            response = client.post(
                "/embed",
                json={"texts": ["text"], "model": "fake", "prompt_name": "query"},
            )
            assert response.status_code == 200
            results.append(response.get_json())

    try:
        callers = [threading.Thread(target=_request) for _ in range(2)]
        for caller in callers:
            caller.start()
            caller.join(timeout=2)
            assert not caller.is_alive()
    finally:
        executor.shutdown(wait=True)

    assert len(results) == 2
    assert all(result["vectors"] == [[1.0, 1.0]] for result in results)
    assert len(worker_threads) == 2
    assert worker_threads[0] is worker_threads[1]
    assert worker_threads[0] not in callers
    assert worker_threads[0].name.startswith("test-embedding-encode")
    assert admission_threads == callers
