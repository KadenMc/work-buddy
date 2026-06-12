"""Each active index partition ships a well-formed ``index-<partition>-refresh`` cron job,
and the rebuild op self-skips a partition whose build is already running."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.sidecar.scheduler.jobs import load_jobs
from work_buddy.sidecar.scheduler.cron import parse_cron_field

_REPO = Path(__file__).resolve().parents[2]
_JOBS_DIR = _REPO / "sidecar_jobs"
_PARTITIONS = ["knowledge", "chrome", "summary", "conversation", "vault"]
_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]  # min hour dom mon dow


@pytest.fixture(scope="module")
def jobs_by_name():
    return {j.name: j for j in load_jobs(_JOBS_DIR, source="system")}


@pytest.mark.parametrize("partition", _PARTITIONS)
def test_index_refresh_job_well_formed(jobs_by_name, partition):
    job = jobs_by_name.get(f"index-{partition}-refresh")
    assert job is not None, f"missing sidecar_jobs/index-{partition}-refresh.md"
    assert job.job_type == "capability"
    assert job.capability == "index_rebuild"
    assert job.params.get("partition") == partition
    assert job.params.get("force") is False  # incremental, never force, on a recurring job
    assert job.recurring is True
    fields = job.schedule.split()
    assert len(fields) == 5, f"index-{partition}-refresh: 5-field cron, got {job.schedule!r}"
    for fld, (lo, hi) in zip(fields, _RANGES):
        assert parse_cron_field(fld, lo, hi), f"index-{partition}-refresh: bad cron field {fld!r}"


def test_refresh_job_jitters_distinct(jobs_by_name):
    jits = [jobs_by_name[f"index-{p}-refresh"].jitter_seconds for p in _PARTITIONS]
    assert len(set(jits)) == len(jits), f"jitter collision among index-refresh jobs: {jits}"


def test_legacy_consolidated_knowledge_job_removed(jobs_by_name):
    assert "consolidated-knowledge-rebuild" not in jobs_by_name


def test_op_self_skips_when_build_in_progress(monkeypatch):
    """The op probes the per-partition lock and bails out instead of blocking-then-erroring."""
    from work_buddy.mcp_server.ops import index_ops
    from work_buddy.index.config import IndexConfig

    monkeypatch.setattr(
        "work_buddy.index.config.load_index_config",
        lambda *a, **k: IndexConfig(enabled=True),
    )
    monkeypatch.setattr("work_buddy.utils.index_lock.is_locked", lambda *a, **k: True)
    out = json.loads(index_ops._index_rebuild_dispatch(partition="chrome"))
    assert out.get("skipped") == "build_in_progress"
    assert out.get("partition") == "chrome"


def test_op_self_skips_against_a_real_held_lock(tmp_path, monkeypatch):
    """End-to-end, hermetic (no build/GPU): a genuinely held per-partition lock makes the
    op skip FAST — a clean instant skip, not the builder's 30s blocking-acquire-then-error.
    A guard-rail makes a regression (falling through to a real build) fail loud and fast."""
    import time
    from work_buddy.mcp_server.ops import index_ops
    from work_buddy.index.config import IndexConfig
    from work_buddy.utils import index_lock

    cfg = IndexConfig(enabled=True, db_path=tmp_path / "idx.db")
    monkeypatch.setattr("work_buddy.index.config.load_index_config", lambda *a, **k: cfg)

    def _boom(*a, **k):
        raise AssertionError("guard failed to skip — op fell through to a real build")

    monkeypatch.setattr("work_buddy.index.partitioned.UnifiedIndex.build", _boom)

    db = cfg.resolved_db_path()
    target = db.parent / f"{db.name}.summary"  # exactly what the op probes
    with index_lock.index_lock(target):  # simulate an in-flight summary build
        t0 = time.time()
        out = json.loads(index_ops._index_rebuild_dispatch(partition="summary"))
        dt = time.time() - t0
    assert out == {"skipped": "build_in_progress", "partition": "summary"}
    assert dt < 5.0  # instant skip, not the 30s blocking acquire
