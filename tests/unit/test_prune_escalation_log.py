"""Tests for ``work_buddy.artifacts.prune_escalation_log``."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from work_buddy.artifacts import prune_escalation_log


def _write_records(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _now_iso(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).isoformat(
        timespec="milliseconds",
    )


def test_prune_drops_old_records(tmp_path):
    log = tmp_path / "escalations.log"
    _write_records(log, [
        {"timestamp": _now_iso(-60), "source": "llm_runner",
         "final_outcome": "success"},
        {"timestamp": _now_iso(-29), "source": "llm_runner",
         "final_outcome": "success"},
        {"timestamp": _now_iso(-1), "source": "llm_runner",
         "final_outcome": "success"},
    ])
    result = prune_escalation_log(log, {"window_days": 30}, dry_run=False)
    assert result["pruned"] == 1
    assert result["remaining"] == 2

    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    timestamps = [r["timestamp"] for r in rows]
    assert all(ts > _now_iso(-30) for ts in timestamps)


def test_prune_dry_run_does_not_modify(tmp_path):
    log = tmp_path / "escalations.log"
    _write_records(log, [
        {"timestamp": _now_iso(-60), "source": "llm_runner",
         "final_outcome": "success"},
    ])
    original_bytes = log.read_bytes()
    result = prune_escalation_log(log, {"window_days": 30}, dry_run=True)
    assert result["pruned"] == 1
    assert log.read_bytes() == original_bytes


def test_prune_no_op_when_all_records_recent(tmp_path):
    log = tmp_path / "escalations.log"
    _write_records(log, [
        {"timestamp": _now_iso(-1), "source": "llm_runner",
         "final_outcome": "success"},
        {"timestamp": _now_iso(0), "source": "llm_runner",
         "final_outcome": "success"},
    ])
    result = prune_escalation_log(log, {"window_days": 30}, dry_run=False)
    assert result["pruned"] == 0
    assert result["remaining"] == 2


def test_prune_missing_file_is_safe(tmp_path):
    result = prune_escalation_log(tmp_path / "nope.log", {}, dry_run=False)
    assert result == {"pruned": 0, "remaining": 0,
                       "bytes_before": 0, "bytes_after": 0}


def test_prune_preserves_malformed_lines(tmp_path):
    log = tmp_path / "escalations.log"
    log.write_text(
        json.dumps({"timestamp": _now_iso(-60), "source": "llm_runner"}) + "\n"
        + "{not json}\n"
        + json.dumps({"timestamp": _now_iso(-1), "source": "llm_runner"}) + "\n",
        encoding="utf-8",
    )
    result = prune_escalation_log(log, {"window_days": 30}, dry_run=False)
    # Old record pruned; malformed line preserved; recent record kept.
    assert result["pruned"] == 1
    text = log.read_text(encoding="utf-8")
    assert "{not json}" in text


def test_prune_keeps_records_without_timestamp(tmp_path):
    """Records missing timestamp survive — defensive against partial writes."""
    log = tmp_path / "escalations.log"
    _write_records(log, [
        {"source": "llm_runner", "final_outcome": "success"},  # no timestamp
        {"timestamp": _now_iso(-60), "source": "llm_runner"},
    ])
    result = prune_escalation_log(log, {"window_days": 30}, dry_run=False)
    assert result["pruned"] == 1  # only the dated old record


def test_prune_default_window_days_30(tmp_path):
    """Default window is 30 days when config doesn't specify."""
    log = tmp_path / "escalations.log"
    _write_records(log, [
        {"timestamp": _now_iso(-31), "source": "llm_runner"},
        {"timestamp": _now_iso(-29), "source": "llm_runner"},
    ])
    result = prune_escalation_log(log, {}, dry_run=False)
    assert result["pruned"] == 1
    assert result["remaining"] == 1


def test_prune_registered_via_artifact_registry():
    """Sanity: ``escalations-log`` is registered as an Artifact.

    Updated for the artifact-system unification (t-aade2f16):
    ``paths.PRUNERS`` is now empty (deprecated). The escalation-log
    pruner is registered as an Artifact in the unified registry. We
    call the registration helper directly here rather than rely on the
    module's import-time side-effect, since other tests may have
    cleared the registry after the module was first loaded.
    """
    import work_buddy.llm.escalation_log as escalation_log_mod
    from work_buddy.artifacts import get_artifact

    escalation_log_mod._register_escalation_log_artifact()
    artifact = get_artifact("escalations-log")
    assert artifact is not None
    desc = artifact.describe()
    assert desc["storage_kind"] == "JsonlStorage"
    assert "TimeWindow" in desc["lifecycle_kind"]
    assert "Delete" in desc["lifecycle_kind"]
