"""Atomic + self-healing IR vector store.

Pins the contract that a crash-truncated ``.npz`` can never take a search
source dark:

- ``save_vectors`` writes atomically — no leftover temp on success, and a
  simulated crashed writer never corrupts the canonical file;
- ``load_vectors`` returns ``None`` (never raises) on a missing / 0-byte /
  truncated file, so callers degrade to BM25 and the next build regenerates;
- ``recover_vector_store`` quarantines corrupt canonicals and reaps orphaned
  write temps, while sparing a live writer's temp;
- a reachable-but-failing ``/ir/index`` surfaces the *real* error instead of
  the generic "service unavailable" (and never the phantom ``WB-Embedding``
  scheduled task).
"""

from __future__ import annotations

import io
import json
import os
import time
from urllib.error import HTTPError, URLError

import numpy as np
import pytest

from work_buddy.ir.store import (
    ORPHAN_TEMP_MAX_AGE_S,
    _npz_path,
    load_vectors,
    recover_vector_store,
    save_vectors,
)
from work_buddy.utils.npz_io import temp_path_for


@pytest.fixture
def tmp_ir_store(tmp_path, monkeypatch):
    """Point the IR store (db + companion ``.npz`` files) at ``tmp_path``.

    ``_npz_path`` derives from ``_db_path``, so patching the latter redirects the
    vector files too — matching the ``tmp_ir_db`` convention in
    ``test_ir_store_metadata_filter.py``.
    """
    db_file = tmp_path / "work_buddy_ir.db"
    monkeypatch.setattr("work_buddy.ir.store._db_path", lambda cfg=None: db_file)
    return tmp_path


def _save(source: str = "conversation", n: int = 5, dim: int = 8):
    # Integer values < 2048 are exact in float16, so the round-trip is lossless.
    vectors = np.arange(n * dim, dtype=np.float32).reshape(n, dim)
    doc_ids = [f"{source}:{i}" for i in range(n)]
    save_vectors(vectors, doc_ids, source=source)
    return vectors, doc_ids


# ---------------------------------------------------------------------------
# save_vectors / load_vectors
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_ir_store):
    vectors, doc_ids = _save()
    npz = _npz_path(None, source="conversation")
    assert npz.exists()
    # Atomic write leaves no temp behind on success.
    assert list(tmp_ir_store.glob("*.tmp.npz")) == []

    loaded = load_vectors(source="conversation")
    assert loaded is not None
    lv, lids = loaded
    assert lids == doc_ids
    np.testing.assert_array_equal(lv, vectors)


def test_load_missing_returns_none(tmp_ir_store):
    assert load_vectors(source="conversation") is None


def test_load_zero_byte_returns_none(tmp_ir_store):
    npz = _npz_path(None, source="conversation")
    npz.write_bytes(b"")
    assert load_vectors(source="conversation") is None


def test_load_truncated_returns_none(tmp_ir_store):
    npz = _npz_path(None, source="conversation")
    # A plausible-looking but truncated zip — surfaces as zipfile.BadZipFile,
    # which is why the catch keeps it explicit.
    npz.write_bytes(b"PK\x03\x04 truncated, not a real npz")
    assert load_vectors(source="conversation") is None


# ---------------------------------------------------------------------------
# recover_vector_store — corrupt canonicals
# ---------------------------------------------------------------------------


def test_recover_quarantines_zero_byte(tmp_ir_store):
    npz = _npz_path(None, source="conversation")
    npz.write_bytes(b"")

    summary = recover_vector_store(cfg={})

    assert str(npz) in summary["quarantined"]
    assert not npz.exists()
    assert npz.with_name(npz.name + ".corrupt").exists()
    # And the next read is clean.
    assert load_vectors(source="conversation") is None


def test_recover_quarantines_truncated(tmp_ir_store):
    npz = _npz_path(None, source="conversation")
    npz.write_bytes(b"PK\x03\x04 truncated")

    summary = recover_vector_store(cfg={})

    assert str(npz) in summary["quarantined"]
    assert npz.with_name(npz.name + ".corrupt").exists()


def test_recover_spares_healthy_canonical(tmp_ir_store):
    _save()
    npz = _npz_path(None, source="conversation")

    summary = recover_vector_store(cfg={})

    assert summary["quarantined"] == []
    assert npz.exists()
    assert load_vectors(source="conversation") is not None


def test_recover_overwrites_prior_corrupt(tmp_ir_store):
    npz = _npz_path(None, source="conversation")
    corrupt = npz.with_name(npz.name + ".corrupt")
    corrupt.write_bytes(b"older forensic copy")
    npz.write_bytes(b"")

    recover_vector_store(cfg={})

    # Exactly one forensic copy is kept (the latest), not an accumulation.
    assert corrupt.exists()
    assert corrupt.read_bytes() == b""


# ---------------------------------------------------------------------------
# recover_vector_store — orphaned vs live temps
# ---------------------------------------------------------------------------


def test_recover_removes_orphan_temp_dead_pid(tmp_ir_store):
    npz = _npz_path(None, source="conversation")
    orphan = temp_path_for(npz, pid=2147480000)  # almost certainly not running
    orphan.write_bytes(b"partial write")

    summary = recover_vector_store(cfg={})

    assert str(orphan) in summary["temps_removed"]
    assert not orphan.exists()


def test_recover_spares_live_temp(tmp_ir_store):
    npz = _npz_path(None, source="conversation")
    # This test process is alive, and the temp is fresh.
    live = temp_path_for(npz, pid=os.getpid())
    live.write_bytes(b"in progress")

    summary = recover_vector_store(cfg={})

    assert str(live) in summary["temps_kept"]
    assert live.exists()


def test_recover_removes_stale_temp_even_if_pid_alive(tmp_ir_store):
    # PID-reuse guard: an alive PID with an old temp is still reaped.
    npz = _npz_path(None, source="conversation")
    stale = temp_path_for(npz, pid=os.getpid())
    stale.write_bytes(b"old")
    old = time.time() - (ORPHAN_TEMP_MAX_AGE_S + 60)
    os.utime(stale, (old, old))

    summary = recover_vector_store(cfg={})

    assert str(stale) in summary["temps_removed"]
    assert not stale.exists()


def test_crash_leaves_canonical_intact(tmp_ir_store):
    # A good canonical plus an orphan temp from a crashed writer: the canonical
    # is untouched and still loads; only the orphan is reaped.
    _vectors, doc_ids = _save()
    npz = _npz_path(None, source="conversation")
    orphan = temp_path_for(npz, pid=2147480000)
    orphan.write_bytes(b"partial write")

    assert load_vectors(source="conversation") is not None

    summary = recover_vector_store(cfg={})

    assert npz.exists()
    assert str(orphan) in summary["temps_removed"]
    loaded = load_vectors(source="conversation")
    assert loaded is not None and loaded[1] == doc_ids


# ---------------------------------------------------------------------------
# /ir/index error surfacing (client + dispatch)
# ---------------------------------------------------------------------------


def _http_500(body: dict):
    def fake_urlopen(req, timeout=30):
        raise HTTPError(req.full_url, 500, "Internal Server Error", {},
                        io.BytesIO(json.dumps(body).encode()))
    return fake_urlopen


def _conn_refused():
    def fake_urlopen(req, timeout=30):
        raise URLError("Connection refused")
    return fake_urlopen


def test_ir_index_surfaces_http_error(monkeypatch):
    import work_buddy.embedding.client as c

    monkeypatch.setattr(c, "urlopen",
                        _http_500({"error": "EOFError: No data left in file"}))
    result = c.ir_index("status", source="conversation")
    assert result == {"error": "EOFError: No data left in file", "status": 500}


def test_request_default_caller_still_none_on_http_error(monkeypatch):
    # A non-ir_index caller (default return_http_error=False) keeps its
    # byte-for-byte contract: an HTTP error still degrades to None.
    import work_buddy.embedding.client as c

    monkeypatch.setattr(c, "urlopen", _http_500({"error": "boom"}))
    assert c._request("POST", "/embed", {"x": 1}) is None


def test_ir_index_none_when_unreachable(monkeypatch):
    import work_buddy.embedding.client as c

    monkeypatch.setattr(c, "urlopen", _conn_refused())
    assert c.ir_index("status", source="conversation") is None


def test_dispatch_surfaces_real_error_not_generic(monkeypatch):
    import work_buddy.embedding.client as c
    import work_buddy.mcp_server.ops.context_ops  # noqa: F401 — registers the op
    from work_buddy.mcp_server.op_registry import get_op

    monkeypatch.setattr(c, "urlopen",
                        _http_500({"error": "EOFError: No data left in file"}))
    dispatch = get_op("op.wb.ir_index")
    assert dispatch is not None

    out = dispatch(action="status", source="conversation")
    payload = json.loads(out)
    assert "EOFError" in payload["error"]
    assert "/ir/index failed" in payload["error"]
    # The old, doubly-wrong message must be gone.
    assert "WB-Embedding" not in out
    assert "unavailable" not in out.lower()


def test_dispatch_unreachable_points_at_sidecar(monkeypatch):
    import work_buddy.embedding.client as c
    import work_buddy.mcp_server.ops.context_ops  # noqa: F401
    from work_buddy.mcp_server.op_registry import get_op

    monkeypatch.setattr(c, "urlopen", _conn_refused())
    dispatch = get_op("op.wb.ir_index")
    assert dispatch is not None

    out = dispatch(action="status", source="conversation")
    payload = json.loads(out)
    assert "sidecar" in payload["error"].lower()
    assert "WB-Embedding" not in out
