"""Off-machine backup push + remote-retention regression guards.

These pin two data-safety failures in the GitHub Releases backup path:

1. The remote retention sweep must bucket releases by their *snapshot
   time* (parsed from the ``snap-<isots>`` tag), never by the ``gh``
   release ``createdAt`` field. ``createdAt`` is the tagged commit's
   date — identical for every release in a data-only repo — so keying
   retention on it collapses every rolling snapshot into one bucket and
   the sweep deletes all but one off-machine copy.

2. A transient network/DNS fault on the push must be retried in-process
   so the *current* snapshot still lands off-machine, rather than being
   abandoned until the next (different) hourly snapshot.
"""

from __future__ import annotations

import json
import types

import pytest

from work_buddy.backups import remote


# ─── Fixtures / helpers ─────────────────────────────────────────────


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    return types.SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _hourly_tags(day: str, hours: range) -> list[str]:
    return [f"snap-{day}T{h:02d}-00-00Z" for h in hours]


# ─── Remote retention keys off the tag, not gh's createdAt ──────────


def test_remote_retention_keeps_full_hourly_tier_under_constant_timestamp(
    monkeypatch,
):
    """The over-pruning regression: with every release reporting an
    identical timestamp (the data-repo's single commit date), the sweep
    must still retain the whole tiered set because it parses the
    snapshot time from the tag — not 1 rolling snapshot."""
    rolling = _hourly_tags("2026-05-20", range(0, 24)) + _hourly_tags(
        "2026-05-19", range(18, 24)
    )
    manual = ["snap-2026-05-11T10-00-00Z-manual",
              "snap-2026-05-12T10-00-00Z-manual"]
    all_tags = rolling + manual

    deleted: list[str] = []

    def fake_run_gh(cmd, *, op_label, repo, tag):
        if op_label == "list":
            # Every release reports the SAME timestamp — the exact
            # condition that collapsed retention to one bucket.
            payload = [
                {"tagName": t, "publishedAt": "2026-05-11T15:23:03Z"}
                for t in all_tags
            ]
            return {"status": "ok", "gh_stdout": json.dumps(payload)}
        if op_label == "delete":
            deleted.append(tag)
            return {"status": "ok"}
        raise AssertionError(f"unexpected gh op {op_label}")

    monkeypatch.setattr(remote, "_run_gh", fake_run_gh)

    result = remote.prune_remote_snapshots(repo="u/r")

    assert result["status"] == "ok"
    # Hourly tier (cap 24) keeps every 2026-05-20 snapshot; the daily
    # tier keeps the newest 2026-05-19 snapshot; the rest are pruned.
    expected_pruned = {f"snap-2026-05-19T{h:02d}-00-00Z" for h in range(18, 23)}
    assert set(result["pruned"]) == expected_pruned
    assert set(deleted) == expected_pruned
    # 24 hourly + 1 daily + 2 manual retained — emphatically not 1+2.
    assert len(result["kept"]) == 27
    for t in _hourly_tags("2026-05-20", range(0, 24)):
        assert t in result["kept"]
    assert "snap-2026-05-19T23-00-00Z" in result["kept"]
    for t in manual:
        assert t in result["kept"]


def test_remote_retention_noop_when_nothing_out_of_bucket(monkeypatch):
    """A small remote set (under every tier cap) prunes nothing."""

    def fake_run_gh(cmd, *, op_label, repo, tag):
        if op_label == "list":
            payload = [
                {"tagName": "snap-2026-05-20T15-00-03Z",
                 "publishedAt": "2026-05-11T15:23:03Z"},
                {"tagName": "snap-2026-05-11T10-00-00Z-manual",
                 "publishedAt": "2026-05-11T15:23:03Z"},
            ]
            return {"status": "ok", "gh_stdout": json.dumps(payload)}
        raise AssertionError(f"delete should not run; got {op_label}")

    monkeypatch.setattr(remote, "_run_gh", fake_run_gh)
    result = remote.prune_remote_snapshots(repo="u/r")
    assert result["status"] == "ok"
    assert result["pruned"] == []


def test_remote_retention_leaves_unparseable_tags_untouched(monkeypatch):
    """A release whose tag does not parse is never deleted."""
    deleted: list[str] = []

    def fake_run_gh(cmd, *, op_label, repo, tag):
        if op_label == "list":
            payload = [
                {"tagName": "snap-not-a-timestamp", "publishedAt": "x"},
                {"tagName": "snap-2026-05-20T15-00-03Z",
                 "publishedAt": "2026-05-11T15:23:03Z"},
            ]
            return {"status": "ok", "gh_stdout": json.dumps(payload)}
        if op_label == "delete":
            deleted.append(tag)
            return {"status": "ok"}
        raise AssertionError(op_label)

    monkeypatch.setattr(remote, "_run_gh", fake_run_gh)
    result = remote.prune_remote_snapshots(repo="u/r")
    assert "snap-not-a-timestamp" not in deleted
    assert result["pruned"] == []


def test_list_remote_snapshots_reports_published_at(monkeypatch):
    """list_remote_snapshots exposes publishedAt (the real push time),
    parses the manual suffix, and synthesizes the release URL."""

    def fake_run_gh(cmd, *, op_label, repo, tag):
        assert "tagName,publishedAt" in cmd  # createdAt is not queried
        payload = [
            {"tagName": "snap-2026-05-20T15-00-03Z",
             "publishedAt": "2026-05-20T15:00:09Z"},
            {"tagName": "snap-2026-05-11T10-00-00Z-manual",
             "publishedAt": "2026-05-11T15:41:01Z"},
        ]
        return {"status": "ok", "gh_stdout": json.dumps(payload)}

    monkeypatch.setattr(remote, "_run_gh", fake_run_gh)
    snaps = remote.list_remote_snapshots(repo="u/r")
    assert snaps[0]["tag"] == "snap-2026-05-20T15-00-03Z"
    assert snaps[0]["published_at"] == "2026-05-20T15:00:09Z"
    assert snaps[0]["manual"] is False
    assert snaps[0]["url"].endswith("/releases/tag/snap-2026-05-20T15-00-03Z")
    assert snaps[1]["manual"] is True


# ─── _run_gh transient classification ───────────────────────────────


def test_run_gh_classifies_windows_dns_failure_as_network(monkeypatch):
    """The observed Windows DNS fault must classify as gh_network."""
    dns_err = (
        'Post "https://uploads.github.com/repos/u/r/releases/1/assets": '
        "dial tcp: lookup uploads.github.com: getaddrinfow: "
        "The requested name is valid, but no data of the requested type "
        "was found."
    )
    monkeypatch.setattr(
        remote.subprocess, "run",
        lambda *a, **k: _fake_proc(1, stderr=dns_err),
    )
    res = remote._run_gh(["gh"], op_label="push", repo="u/r", tag="t")
    assert res["status"] == "gh_network"


def test_run_gh_keeps_auth_failure_permanent(monkeypatch):
    """An auth failure is not misclassified as a transient network fault."""
    monkeypatch.setattr(
        remote.subprocess, "run",
        lambda *a, **k: _fake_proc(1, stderr="You are not logged into any "
                                   "GitHub hosts."),
    )
    res = remote._run_gh(["gh"], op_label="push", repo="u/r", tag="t")
    assert res["status"] == "gh_unauthenticated"


# ─── push_snapshot retry behaviour ──────────────────────────────────


@pytest.fixture
def snapshot_dir(tmp_path):
    """A snapshot directory with a placeholder tarball present."""
    d = tmp_path / "snap-2026-05-20T16-00-20Z"
    d.mkdir()
    (d / remote.BACKUP_FILENAME).write_bytes(b"tarball")
    return d


def test_push_snapshot_retries_transient_then_succeeds(
    monkeypatch, snapshot_dir,
):
    """A transient gh_network fault is retried; a later attempt lands."""
    monkeypatch.setattr(remote, "_format_release_body", lambda d: "body")
    monkeypatch.setattr(remote.time, "sleep", lambda s: None)

    calls = []

    def fake_run_gh(cmd, *, op_label, repo, tag):
        calls.append(cmd[2])  # "create" / "upload"
        if len(calls) == 1:
            return {"status": "gh_network", "error": "dial tcp"}
        return {"status": "ok", "tag": tag}

    monkeypatch.setattr(remote, "_run_gh", fake_run_gh)
    res = remote.push_snapshot(snapshot_dir, repo="u/r")
    assert res["status"] == "ok"
    assert res["recovered_after_attempts"] == 2
    assert len(calls) == 2


def test_push_snapshot_does_not_retry_permanent_failure(
    monkeypatch, snapshot_dir,
):
    """An unauthenticated failure exits immediately — no wasted retries."""
    monkeypatch.setattr(remote, "_format_release_body", lambda d: "body")
    monkeypatch.setattr(remote.time, "sleep", lambda s: None)

    calls = []

    def fake_run_gh(cmd, *, op_label, repo, tag):
        calls.append(cmd)
        return {"status": "gh_unauthenticated", "error": "not logged into"}

    monkeypatch.setattr(remote, "_run_gh", fake_run_gh)
    res = remote.push_snapshot(snapshot_dir, repo="u/r")
    assert res["status"] == "gh_unauthenticated"
    assert len(calls) == 1


def test_push_snapshot_exhausts_retries_on_persistent_fault(
    monkeypatch, snapshot_dir,
):
    """A persistent network fault exhausts retries and reports honestly."""
    monkeypatch.setattr(remote, "_format_release_body", lambda d: "body")
    monkeypatch.setattr(remote.time, "sleep", lambda s: None)

    calls = []

    def fake_run_gh(cmd, *, op_label, repo, tag):
        calls.append(cmd)
        return {"status": "gh_network", "error": "dial tcp"}

    monkeypatch.setattr(remote, "_run_gh", fake_run_gh)
    res = remote.push_snapshot(snapshot_dir, repo="u/r", max_attempts=3)
    assert res["status"] == "gh_network"
    assert res["attempts"] == 3
    assert len(calls) == 3


def test_push_snapshot_falls_back_to_upload_when_release_exists(
    monkeypatch, snapshot_dir,
):
    """When the release already exists (a prior attempt created it but
    its asset upload failed), the push falls back to upload --clobber so
    a retry converges instead of looping on 'already exists'."""
    monkeypatch.setattr(remote, "_format_release_body", lambda d: "body")
    monkeypatch.setattr(remote.time, "sleep", lambda s: None)

    seen = []

    def fake_run_gh(cmd, *, op_label, repo, tag):
        seen.append(cmd[2])  # release subcommand
        if cmd[2] == "create":
            return {"status": "gh_failed",
                    "error": 'a release with the tag "t" already exists'}
        return {"status": "ok", "tag": tag}

    monkeypatch.setattr(remote, "_run_gh", fake_run_gh)
    res = remote.push_snapshot(snapshot_dir, repo="u/r")
    assert res["status"] == "ok"
    assert seen == ["create", "upload"]
