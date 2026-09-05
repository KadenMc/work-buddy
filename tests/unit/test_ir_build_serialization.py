"""IR index builds serialize on one DB-wide advisory lock.

Every IR source (conversation, summary, task_note, docs, chrome) shares one
SQLite DB and one embedding-service process, and building is CPU-bound in the
same interpreter that serves ``/ir/search``. Unserialized builds therefore
contend for one writer and one GIL, inflating each other's duration until a
search exceeds its client timeout and reports the service as unavailable.

Pins the exclusion contract:

- a build that arrives while another holds the lock is **skipped, not queued**
  (queuing would hold a request open for the length of someone else's build);
- ``action="status"`` is never gated, so status stays readable during a build;
- the op-level probe short-circuits before the HTTP round trip;
- the bulk-build sweep holds the lock across every source.
"""

from __future__ import annotations

import threading

import pytest

from work_buddy.utils.index_lock import index_lock, is_locked


@pytest.fixture
def tmp_ir_db(tmp_path, monkeypatch):
    """Point ``ir.store._db_path`` at a temp file so the lock lands in tmp_path."""
    db = tmp_path / "work_buddy_ir.db"
    monkeypatch.setattr("work_buddy.ir.store._db_path", lambda cfg=None: db)
    return db


@pytest.fixture
def client():
    from work_buddy.embedding.service import app

    app.config.update(TESTING=True)
    return app.test_client()


def _post(client, **body):
    return client.post("/ir/index", json=body)


def test_build_skips_while_another_build_holds_the_lock(client, tmp_ir_db, monkeypatch):
    """A second build reports itself skipped instead of waiting out the first."""
    called = []
    monkeypatch.setattr(
        "work_buddy.ir.store.build_index",
        lambda **kw: called.append(kw) or {"source": kw.get("source")},
    )

    with index_lock(tmp_ir_db):
        resp = _post(client, action="build", source="summary")

    assert resp.status_code == 200
    assert resp.get_json()["result"] == {
        "source": "summary",
        "skipped": True,
        "reason": "build_in_progress",
    }
    # The whole point: no build work ran behind the held lock.
    assert called == []


def test_build_runs_and_releases_the_lock(client, tmp_ir_db, monkeypatch):
    """An uncontended build acquires, builds, and leaves the lock free."""
    monkeypatch.setattr(
        "work_buddy.ir.store.build_index",
        lambda **kw: {"source": kw.get("source"), "docs_inserted": 3},
    )

    resp = _post(client, action="build", source="summary", include_dense=False)

    assert resp.status_code == 200
    result = resp.get_json()["result"]
    assert result["docs_inserted"] == 3
    assert "skipped" not in result
    assert not is_locked(tmp_ir_db), "lock must be released after the build"


def test_lock_is_held_for_the_duration_of_the_build(client, tmp_ir_db, monkeypatch):
    """The lock covers the build body, not just its entry and exit."""
    observed: list[bool] = []

    def _slow_build(**kw):
        observed.append(is_locked(tmp_ir_db))
        return {"source": kw.get("source")}

    monkeypatch.setattr("work_buddy.ir.store.build_index", _slow_build)

    _post(client, action="build", source="summary", include_dense=False)

    assert observed == [True]


def test_dense_encode_runs_inside_the_lock(client, tmp_ir_db, monkeypatch):
    """Index and vectors are written as one unit, so encoding is covered too."""
    observed: list[bool] = []
    monkeypatch.setattr("work_buddy.ir.store.build_index", lambda **kw: {})
    monkeypatch.setattr(
        "work_buddy.ir.dense.build_vectors",
        lambda **kw: observed.append(is_locked(tmp_ir_db)) or {"encoded": 0},
    )

    _post(client, action="build", source="summary", include_dense=True)

    assert observed == [True]


def test_status_is_never_gated_by_the_lock(client, tmp_ir_db, monkeypatch):
    """Status must stay readable while a build holds the lock."""
    monkeypatch.setattr(
        "work_buddy.ir.store.index_status",
        lambda source=None: {"status": "ok", "source": source},
    )

    with index_lock(tmp_ir_db):
        resp = _post(client, action="status", source="summary")

    assert resp.status_code == 200
    assert resp.get_json()["result"] == {"status": "ok", "source": "summary"}


def test_concurrent_builds_do_not_overlap(client, tmp_ir_db, monkeypatch):
    """Two builds racing the endpoint never run their bodies at the same time."""
    # Shrink the wait so the loser gives up quickly rather than queueing.
    monkeypatch.setattr(
        "work_buddy.embedding.service._IR_BUILD_LOCK_TIMEOUT_S", 0.05
    )

    inside = 0
    peak = 0
    guard = threading.Lock()
    entered = threading.Event()

    def _build(**kw):
        nonlocal inside, peak
        with guard:
            inside += 1
            peak = max(peak, inside)
        entered.set()
        # Hold well past the acquire timeout so the second request must skip.
        threading.Event().wait(0.5)
        with guard:
            inside -= 1
        return {"source": kw.get("source")}

    monkeypatch.setattr("work_buddy.ir.store.build_index", _build)

    results: list = []

    def _fire():
        results.append(_post(client, action="build", source="summary",
                             include_dense=False).get_json()["result"])

    first = threading.Thread(target=_fire)
    first.start()
    entered.wait(timeout=5)
    second = threading.Thread(target=_fire)
    second.start()
    first.join(timeout=15)
    second.join(timeout=15)

    assert peak == 1, "build bodies overlapped despite the lock"
    assert sum(1 for r in results if r.get("skipped")) == 1
    assert not is_locked(tmp_ir_db)


@pytest.fixture
def no_real_http(monkeypatch):
    """Intercept the embedding client's transport.

    ``context_ops`` binds ``ir_index`` into a closure at import, so the client
    function itself cannot be swapped. ``_request`` is resolved as a module global
    on every call, which makes it the reliable seam.
    """
    calls: list = []

    def _fake(method, path, data=None, **kw):
        calls.append((method, path, data))
        return {"result": {"status": "ok"}}

    monkeypatch.setattr("work_buddy.embedding.client._request", _fake)
    return calls


def test_op_dispatch_skips_before_the_http_round_trip(tmp_ir_db, no_real_http):
    """The op probes the lock read-only and never calls the service when held."""
    import json

    from work_buddy.mcp_server.op_registry import get_op, load_builtin_ops

    # Idempotent, and re-registers if an earlier test cleared the registry, so
    # this test does not depend on selection order.
    load_builtin_ops()
    dispatch = get_op("op.wb.ir_index")
    assert dispatch is not None

    with index_lock(tmp_ir_db):
        out = json.loads(dispatch(action="build", source="conversation"))

    assert out == {
        "source": "conversation",
        "skipped": True,
        "reason": "build_in_progress",
    }
    assert no_real_http == [], "op must not reach the service while a build is running"


def test_op_dispatch_status_is_not_gated(tmp_ir_db, no_real_http):
    """A status call still reaches the service while a build runs."""
    import json

    from work_buddy.mcp_server.op_registry import get_op, load_builtin_ops

    # Idempotent, and re-registers if an earlier test cleared the registry, so
    # this test does not depend on selection order.
    load_builtin_ops()
    dispatch = get_op("op.wb.ir_index")
    assert dispatch is not None

    with index_lock(tmp_ir_db):
        out = json.loads(dispatch(action="status", source="conversation"))

    assert out == {"status": "ok"}
    assert [p for _, p, _ in no_real_http] == ["/ir/index"]


def test_bulk_build_holds_the_lock_across_every_source(tmp_ir_db, monkeypatch):
    """The bulk sweep cannot interleave with the scheduled per-source builds."""
    observed: list[bool] = []
    monkeypatch.setattr(
        "work_buddy.ir.store.build_index",
        lambda **kw: observed.append(is_locked(tmp_ir_db)) or {},
    )
    monkeypatch.setattr("work_buddy.ir.dense.build_vectors", lambda **kw: {})

    from work_buddy.indexing.adapters.ir import IRIndexAdapter

    result = IRIndexAdapter().bulk_build()

    assert result.ok, result.error
    assert observed and all(observed), "lock must be held for every source"
    assert not is_locked(tmp_ir_db)
