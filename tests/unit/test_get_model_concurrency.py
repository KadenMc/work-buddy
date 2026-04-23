"""Invariants for the refactored ``_get_model()`` lock design.

Before the refactor, ``_registry_lock`` was held for the full duration of
``_load_model()``. That meant a cold load of the big passage encoder
(``leaf-ir``, ~500 MB, 5+ seconds to instantiate) blocked every other
thread calling ``_get_model('leaf-ir-query')`` — even though those
threads want a completely different model. Concurrent semantic
searches then piled up behind the bulk-encode fallback and the
dashboard's 30s client timeout tripped.

The refactor moves ``_load_model`` outside any held lock and
coordinates concurrent loads via a per-entry ``Condition``. This test
file locks in the observable invariants so we don't regress.
"""

from __future__ import annotations

import threading
import time

import pytest


# ---------------------------------------------------------------------------
# Fixture: fresh registry with two fake entries that we can control the
# load latency of from the test.
# ---------------------------------------------------------------------------


class _FakeModel:
    """Stand-in for a SentenceTransformer that doesn't download anything."""

    def __init__(self, key: str):
        self.key = key

    def get_sentence_embedding_dimension(self) -> int:
        return 768


@pytest.fixture
def controlled_registry(monkeypatch):
    """Register two entries whose loads block on a test-controlled event.

    Yields a tuple ``(release_fn, load_call_count)``:
      * ``release_fn(key)`` — unblock the load of entry ``key``.
      * ``load_call_count[key]`` — how many times ``_load_model`` was
        invoked for that key. Used to assert no double-loads.
    """
    import work_buddy.embedding.service as svc

    # Reset registry to a clean slate so other tests don't leak in.
    svc._registry.clear()
    svc._init_registry({
        "embedding": {
            "models": {
                "alpha": {"name": "fake/alpha", "dims": 768, "eager": False},
                "beta":  {"name": "fake/beta",  "dims": 768, "eager": False},
            },
            "default_model": "alpha",
        },
    })

    events: dict[str, threading.Event] = {
        "alpha": threading.Event(),
        "beta": threading.Event(),
    }
    call_count: dict[str, int] = {"alpha": 0, "beta": 0}

    def _fake_load(entry):
        # Count invocations so tests can assert no double-load.
        call_count[entry.key] += 1
        # Block until the test releases us.
        if not events[entry.key].wait(timeout=10.0):
            entry.status = "error"
            entry.error = "test timeout waiting for release"
            return
        entry.model = _FakeModel(entry.key)
        entry.status = "loaded"
        entry.load_time_s = 0.0

    monkeypatch.setattr(svc, "_load_model", _fake_load)

    yield events, call_count

    # Drain: release anything still blocked so threads don't dangle.
    for ev in events.values():
        ev.set()
    svc._registry.clear()


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_fast_path_no_lock_acquired_when_already_loaded(
    controlled_registry, monkeypatch
):
    """Already-loaded entries must return without touching load_cond.

    Regression guard: the fast path exists specifically so a hot
    query doesn't pay even an uncontended lock-acquire on every call.
    """
    import work_buddy.embedding.service as svc

    events, _ = controlled_registry
    events["alpha"].set()  # allow load to proceed
    _ = svc._get_model("alpha")  # first call loads

    # Now monkeypatch the condition to detect acquisition. If the fast
    # path acquires, the acquire() call would raise.
    entry = svc._registry["alpha"]
    acquired = []

    class _TrippedCond:
        def __enter__(self_):
            acquired.append(True)
            return self_

        def __exit__(self_, *a):
            return False

        def wait(self_, *a, **kw):
            acquired.append("wait")

        def notify_all(self_):
            acquired.append("notify")

    monkeypatch.setattr(entry, "load_cond", _TrippedCond())
    m = svc._get_model("alpha")
    assert m is not None
    assert acquired == [], (
        "Fast path must not touch load_cond when model is already loaded"
    )


def test_concurrent_load_of_different_models_does_not_block(
    controlled_registry,
):
    """Thread loading 'alpha' must NOT block a thread loading 'beta'.

    This is the core invariant. Pre-refactor, the global lock
    serialized these and the dashboard-search bug was downstream of
    that serialization. The whole refactor exists for this test.
    """
    import work_buddy.embedding.service as svc

    events, call_count = controlled_registry
    results: dict[str, object] = {}
    errors: list[Exception] = []

    def _load(key):
        try:
            results[key] = svc._get_model(key)
        except Exception as exc:  # pragma: no cover — diagnostic only
            errors.append(exc)

    # Start both loads.
    t_alpha = threading.Thread(target=_load, args=("alpha",))
    t_beta = threading.Thread(target=_load, args=("beta",))
    t_alpha.start()
    t_beta.start()

    # Give both threads a moment to enter their respective _load_model.
    time.sleep(0.05)

    # Release beta FIRST. If the old global-lock design were still
    # here, t_beta would be stuck waiting on _registry_lock held by
    # t_alpha — and this test would time out.
    events["beta"].set()
    t_beta.join(timeout=3.0)
    assert not t_beta.is_alive(), (
        "beta load did not complete while alpha load was in flight — "
        "indicates the loads are still serialized (regression)"
    )
    assert "beta" in results
    assert results["beta"].key == "beta"

    # Now release alpha and clean up.
    events["alpha"].set()
    t_alpha.join(timeout=3.0)
    assert results["alpha"].key == "alpha"
    assert not errors, f"unexpected errors: {errors}"


def test_concurrent_same_model_loads_exactly_once(controlled_registry):
    """N threads asking for the same model → exactly one _load_model call.

    The coordination-via-Condition pattern must not double-load. Other
    threads wait for the loader to finish and then get the same
    reference.
    """
    import work_buddy.embedding.service as svc

    events, call_count = controlled_registry
    N = 5
    results: list[object] = []
    results_lock = threading.Lock()

    def _load():
        m = svc._get_model("alpha")
        with results_lock:
            results.append(m)

    threads = [threading.Thread(target=_load) for _ in range(N)]
    for t in threads:
        t.start()

    time.sleep(0.1)  # let all threads converge on the condition

    # Only ONE thread should be actually loading; others park on cond.
    events["alpha"].set()

    for t in threads:
        t.join(timeout=3.0)
        assert not t.is_alive(), "thread failed to complete"

    assert call_count["alpha"] == 1, (
        f"Expected exactly 1 _load_model call, got {call_count['alpha']} "
        "— loads are not deduplicated"
    )
    assert len(results) == N
    # All threads must get the same model reference.
    first = results[0]
    for r in results[1:]:
        assert r is first, "different model instances returned to concurrent callers"


def test_load_failure_propagates_to_waiters(monkeypatch):
    """If _load_model raises, waiting threads must get a RuntimeError, not hang."""
    import work_buddy.embedding.service as svc

    svc._registry.clear()
    svc._init_registry({
        "embedding": {
            "models": {"broken": {"name": "fake/broken", "dims": 768, "eager": False}},
            "default_model": "broken",
        },
    })

    loader_ready = threading.Event()
    loader_can_fail = threading.Event()

    def _fake_load(entry):
        loader_ready.set()
        loader_can_fail.wait(timeout=5.0)
        entry.status = "error"
        entry.error = "simulated failure"
        entry.model = None

    monkeypatch.setattr(svc, "_load_model", _fake_load)

    results: list[Exception | object] = []
    results_lock = threading.Lock()

    def _attempt():
        try:
            results.append(svc._get_model("broken"))
        except Exception as exc:
            with results_lock:
                results.append(exc)

    # T1 starts the load and blocks in _fake_load.
    t1 = threading.Thread(target=_attempt)
    t1.start()
    assert loader_ready.wait(timeout=3.0)

    # T2 tries to get the same model — should park on the condition.
    t2 = threading.Thread(target=_attempt)
    t2.start()
    time.sleep(0.1)  # let T2 reach the wait

    # T1's load fails.
    loader_can_fail.set()

    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert len(results) == 2
    for r in results:
        assert isinstance(r, RuntimeError), (
            f"expected RuntimeError on failed load, got {type(r).__name__}: {r}"
        )
        assert "simulated failure" in str(r)

    # Additionally, the entry should not be stuck in "loading" state.
    entry = svc._registry["broken"]
    assert entry._loading is False
    assert entry.status == "error"

    svc._registry.clear()


def test_load_exception_resets_loading_flag(monkeypatch):
    """If _load_model raises outright (not just fails-with-status), the
    _loading flag must still be cleared and waiters notified — no
    permanent-loading zombies.
    """
    import work_buddy.embedding.service as svc

    svc._registry.clear()
    svc._init_registry({
        "embedding": {
            "models": {"explodes": {"name": "fake/x", "dims": 768, "eager": False}},
            "default_model": "explodes",
        },
    })

    def _fake_load(entry):
        # Raise BEFORE setting status — simulates an unexpected
        # exception type that escapes _load_model's try/except
        # (hypothetical, since the real impl catches broadly; this
        # test pins the finally-block behavior anyway).
        raise RuntimeError("unexpected")

    monkeypatch.setattr(svc, "_load_model", _fake_load)

    with pytest.raises(RuntimeError):
        svc._get_model("explodes")

    entry = svc._registry["explodes"]
    assert entry._loading is False, (
        "Loader exception left entry in permanent 'loading' state — "
        "future _get_model calls would hang forever"
    )

    svc._registry.clear()
