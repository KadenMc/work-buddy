"""Tests for the ``service-logs`` artifact (subprocess stdout/stderr retention).

The daemon rolls an oversized live service log aside at startup; this artifact
owns RETENTION — age-deleting rolled-aside backups while pinning each service's
live ``<name>.log`` (name-based, so a crashed service's last log survives too).

These exercise:
    1. The pin predicate (``_is_live_service_log``) across name shapes.
    2. End-to-end ``Artifact.prune`` over a ``DirShape.LOG_FILES`` tree:
       aged backups reaped, fresh backups and all live logs preserved.
    3. The default registration is present and swept by ``sweep_all``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from work_buddy.artifacts import (
    Artifact,
    Delete,
    DirectoryTreeStorage,
    DirShape,
    Lifecycle,
    MtimeWindow,
)
from work_buddy.artifacts.default_registrations import (
    _is_live_log_file,
    register_default_artifacts,
)


def _age(path, days: float) -> None:
    """Backdate a file's mtime by ``days``."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    os.utime(path, (ts, ts))


def _service_logs_artifact(root) -> Artifact:
    """Build the artifact exactly as ``register_service_logs_artifact`` does,
    but rooted at a tmp dir."""
    return Artifact(
        name="service-logs",
        storage=DirectoryTreeStorage(
            root=root, shape=DirShape.LOG_FILES, artifact_name="service-logs",
        ),
        lifecycle=Lifecycle(
            trigger=MtimeWindow(mtime_field="_mtime", max_age_days=7),
            action=Delete(),
            retention_predicate=_is_live_log_file,
        ),
    )


# --------------------------------------------------------------------------
# pin predicate
# --------------------------------------------------------------------------


def test_pin_predicate_pins_live_logs_only():
    pinned = {
        # service_logs convention (suffix before .log)
        "messaging.log": True,        # live → pinned
        "mcp_gateway.log": True,      # underscore name, still live → pinned
        "messaging.1.log": False,     # legacy numbered backup → reapable
        "messaging.20260601T120000123456.log": False,  # dateext backup → reapable
        "messaging.20260601T120000123456-1.log": False,  # collision-suffixed → reapable
        # agents/logs RotatingFileHandler convention (suffix after .log)
        "telegram.log": True,         # live → pinned
        "telegram.log.1": False,      # rotated backup → reapable
        "telegram.log.2": False,      # rotated backup → reapable
        "work_buddy.log.4": False,    # rotated backup → reapable
        "notalog.txt": False,         # not a .log → not pinned
    }
    for name, expected in pinned.items():
        assert _is_live_log_file({"_file_name": name}) is expected, name


# --------------------------------------------------------------------------
# end-to-end prune
# --------------------------------------------------------------------------


def test_prune_reaps_aged_backups_pins_live(tmp_path):
    # Live logs — fresh mtime, must always survive (pinned).
    live_msg = tmp_path / "messaging.log"
    live_msg.write_text("live messaging output")
    live_emb = tmp_path / "embedding.log"
    live_emb.write_text("live embedding output")

    # A *stopped* service's live log: old mtime but still pinned by name.
    stopped = tmp_path / "telegram.log"
    stopped.write_text("last output before it stopped")
    _age(stopped, 30)

    # Aged backups — must be reaped.
    legacy_orphan = tmp_path / "messaging.1.log"      # legacy numbered
    legacy_orphan.write_text("x" * 1000)
    _age(legacy_orphan, 30)
    dateext_orphan = tmp_path / "dashboard.20260501T120000000000.log"
    dateext_orphan.write_text("x" * 1000)
    _age(dateext_orphan, 30)

    # A *fresh* backup — within the window, so kept (age drives reaping, not name).
    fresh_backup = tmp_path / "dashboard.20260601T120000000000.log"
    fresh_backup.write_text("recent rolled backup")
    _age(fresh_backup, 1)

    result = _service_logs_artifact(tmp_path).prune(dry_run=False)

    assert result.pruned == 2, result
    # Reaped:
    assert not legacy_orphan.exists()
    assert not dateext_orphan.exists()
    # Preserved:
    assert live_msg.exists()
    assert live_emb.exists()
    assert stopped.exists()        # pinned despite 30-day age (name-based)
    assert fresh_backup.exists()   # backup but within window


def test_prune_dry_run_deletes_nothing(tmp_path):
    orphan = tmp_path / "messaging.1.log"
    orphan.write_text("x" * 1000)
    _age(orphan, 30)
    result = _service_logs_artifact(tmp_path).prune(dry_run=True)
    assert orphan.exists()  # dry-run never mutates
    # The survey still identifies it as expired.
    assert result.pruned == 1


# --------------------------------------------------------------------------
# default registration + sweep
# --------------------------------------------------------------------------


def test_service_logs_registered_and_swept():
    from work_buddy.artifacts.registry import get_artifact, sweep_all

    register_default_artifacts()  # idempotent by name
    art = get_artifact("service-logs")
    assert art is not None
    assert art.storage._root.name == "service_logs"

    # Scoped dry-run sweep against the real dir — surveys, never mutates.
    results = sweep_all(dry_run=True, name="service-logs")
    assert len(results) == 1
    assert results[0].artifact_name == "service-logs"
    assert results[0].error is None


def test_agents_logs_registered_and_swept():
    """The agents/logs RotatingFileHandler dir is also governed."""
    from work_buddy.artifacts.registry import get_artifact, sweep_all

    register_default_artifacts()  # idempotent by name
    art = get_artifact("agents-logs")
    assert art is not None
    # root is .data/agents/logs
    assert art.storage._root.name == "logs"
    assert art.storage._root.parent.name == "agents"

    results = sweep_all(dry_run=True, name="agents-logs")
    assert len(results) == 1
    assert results[0].artifact_name == "agents-logs"
    assert results[0].error is None


def test_prune_reaps_rotating_handler_backups(tmp_path):
    """RotatingFileHandler naming (``telegram.log.N``): live pinned, backups reaped by age."""
    live = tmp_path / "telegram.log"
    live.write_text("live telegram output")  # fresh → pinned

    aged_b1 = tmp_path / "telegram.log.1"
    aged_b1.write_text("x" * 1000)
    _age(aged_b1, 30)
    aged_b2 = tmp_path / "telegram.log.2"  # a large rotated backup
    aged_b2.write_text("x" * 5000)
    _age(aged_b2, 30)

    result = _service_logs_artifact(tmp_path).prune(dry_run=False)

    assert result.pruned == 2, result
    assert not aged_b1.exists()
    assert not aged_b2.exists()
    assert live.exists()  # live log pinned by name despite sharing the dir
