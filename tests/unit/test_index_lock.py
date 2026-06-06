"""Tests for the generic per-index advisory lock (`work_buddy/utils/index_lock.py`).

The concurrency/reclaim tests are the load-bearing ones — they guard against a
build racing the cron, a crashed build wedging the lock, and a PID-reused holder.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from work_buddy.utils import index_lock


def _write_lock(target, *, pid, age_s=0.0):
    lock = index_lock._lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps({"pid": pid, "started_at": time.time(), "heartbeat": time.time()}),
        encoding="ascii",
    )
    if age_s:
        old = time.time() - age_s
        os.utime(lock, (old, old))
    return lock


def test_lock_path_is_sibling(tmp_path):
    assert index_lock._lock_path(tmp_path / "vault-index.db") == tmp_path / "vault-index.db.lock"


def test_acquire_release_roundtrip(tmp_path):
    target = tmp_path / "x.db"
    lock = index_lock._lock_path(target)
    assert not lock.exists()
    with index_lock.index_lock(target):
        assert lock.exists()
        assert json.loads(lock.read_text())["pid"] == os.getpid()
    assert not lock.exists()  # released


def test_is_locked_true_while_held_false_after(tmp_path):
    target = tmp_path / "x.db"
    with index_lock.index_lock(target):
        assert index_lock.is_locked(target) is True
    assert index_lock.is_locked(target) is False


def test_is_locked_false_when_no_file(tmp_path):
    assert index_lock.is_locked(tmp_path / "x.db") is False


def test_is_locked_false_dead_pid(tmp_path, monkeypatch):
    target = tmp_path / "x.db"
    _write_lock(target, pid=999999)
    monkeypatch.setattr(index_lock, "is_process_alive", lambda pid: False)
    assert index_lock.is_locked(target) is False


def test_is_locked_false_stale_age(tmp_path, monkeypatch):
    target = tmp_path / "x.db"
    _write_lock(target, pid=os.getpid(), age_s=10_000)
    monkeypatch.setattr(index_lock, "is_process_alive", lambda pid: True)
    assert index_lock.is_locked(target) is False


def test_is_locked_is_read_only(tmp_path, monkeypatch):
    target = tmp_path / "x.db"
    lock = _write_lock(target, pid=os.getpid())
    monkeypatch.setattr(index_lock, "is_process_alive", lambda pid: True)
    before = (lock.stat().st_mtime_ns, lock.read_text())
    index_lock.is_locked(target)
    assert (lock.stat().st_mtime_ns, lock.read_text()) == before


def test_reclaim_dead_pid(tmp_path, monkeypatch):
    target = tmp_path / "x.db"
    _write_lock(target, pid=999999)
    monkeypatch.setattr(index_lock, "is_process_alive", lambda pid: pid == os.getpid())
    with index_lock.index_lock(target, timeout=2):
        assert json.loads(index_lock._lock_path(target).read_text())["pid"] == os.getpid()


def test_reclaim_stale_age_even_if_pid_alive(tmp_path, monkeypatch):
    # PID-reuse guard: the holder PID resolves alive, but the lock is aged out.
    target = tmp_path / "x.db"
    _write_lock(target, pid=12345, age_s=10_000)
    monkeypatch.setattr(index_lock, "is_process_alive", lambda pid: True)
    with index_lock.index_lock(target, timeout=2):
        assert json.loads(index_lock._lock_path(target).read_text())["pid"] == os.getpid()


def test_honor_live_lock_times_out(tmp_path, monkeypatch):
    target = tmp_path / "x.db"
    _write_lock(target, pid=12345)  # fresh
    monkeypatch.setattr(index_lock, "is_process_alive", lambda pid: True)
    with pytest.raises(TimeoutError):
        with index_lock.index_lock(target, timeout=0.3, poll=0.05):
            pass
    # the other holder's lock is untouched
    assert json.loads(index_lock._lock_path(target).read_text())["pid"] == 12345


def test_refresh_keeps_alive_past_short_stale(tmp_path, monkeypatch):
    target = tmp_path / "x.db"
    _write_lock(target, pid=os.getpid(), age_s=5)
    monkeypatch.setattr(index_lock, "is_process_alive", lambda pid: True)
    assert index_lock.is_locked(target, stale_after_s=2) is False  # aged out
    index_lock.refresh(target)
    assert index_lock.is_locked(target, stale_after_s=2) is True   # heartbeat re-armed it


def test_fresh_empty_lock_is_honored(tmp_path):
    # The sub-ms create→write window: an empty/unparseable but FRESH lock must be
    # honored (someone mid-acquire), not stolen.
    target = tmp_path / "x.db"
    lock = index_lock._lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="ascii")
    assert index_lock.is_locked(target) is True
    with pytest.raises(TimeoutError):
        with index_lock.index_lock(target, timeout=0.3, poll=0.05):
            pass


def test_stale_garbage_lock_reclaimed(tmp_path):
    target = tmp_path / "x.db"
    lock = index_lock._lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("garbage", encoding="ascii")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    assert index_lock.is_locked(target) is False
    with index_lock.index_lock(target, timeout=2):
        assert json.loads(lock.read_text())["pid"] == os.getpid()


def test_release_only_unlinks_own_lock(tmp_path):
    target = tmp_path / "x.db"
    lock = index_lock._lock_path(target)
    cm = index_lock.index_lock(target, timeout=2)
    cm.__enter__()
    # A "successor" (different pid) takes over the lock file mid-hold.
    index_lock._write_holder_atomic(
        lock, {"pid": 424242, "started_at": time.time(), "heartbeat": time.time()}
    )
    cm.__exit__(None, None, None)
    assert lock.exists()  # NOT deleted — it's the successor's now
    assert json.loads(lock.read_text())["pid"] == 424242
    lock.unlink()


def test_serializes_within_process(tmp_path):
    # Three threads (same pid) competing for a fresh lock → never two holders at once.
    target = tmp_path / "x.db"
    concurrent = [0]
    peak = [0]
    guard = threading.Lock()
    errors: list[Exception] = []

    def worker():
        try:
            with index_lock.index_lock(target, timeout=5, poll=0.01):
                with guard:
                    concurrent[0] += 1
                    peak[0] = max(peak[0], concurrent[0])
                time.sleep(0.03)
                with guard:
                    concurrent[0] -= 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert peak[0] == 1  # mutual exclusion held
