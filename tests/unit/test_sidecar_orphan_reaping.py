"""Regression: sidecar takeover must reap the old daemon's children.

Background — May 2026: a single dashboard child process (PID 29868)
ran continuously for 16 days through several sidecar restarts. The
daemon log file ``dashboard.1.log`` showed it had started 2026-04-19
and was still binding port 5127 on 2026-05-05, serving stale in-memory
bytecode that pre-dated weeks of merged commits.

Root cause chain:

1. ``takeover_existing_daemon`` calls ``os.kill(pid, signal.SIGTERM)``.
   On Windows this is ``TerminateProcess`` — a hard kill that does NOT
   trigger the daemon's signal handler, so its ``_shutdown`` cleanup
   (which would ``terminate()`` each child) never runs.
2. The orphaned children survive on their bound ports.
3. The new daemon's ``_start_child`` calls ``_kill_process_on_port`` to
   clean up — but if that fails (PID lookup timeout, force-kill blocked),
   the new daemon refuses to spawn and the orphan keeps serving
   ``/health`` against stale bytecode forever.

Fix: kill the daemon's direct children FIRST, then the daemon itself.
This way orphans are never created, regardless of whether the
daemon's signal handler ever fires.

These tests pin that contract.
"""

from __future__ import annotations

from work_buddy import process
from work_buddy.sidecar import pid as sidecar_pid


# ---------------------------------------------------------------------------
# takeover_existing_daemon — orphan-prevention contract
# ---------------------------------------------------------------------------


def test_takeover_kills_children_before_daemon(monkeypatch):
    """The order matters. If we killed the daemon first on Windows,
    ``TerminateProcess`` would hard-kill it, the daemon's signal
    handler would never fire, and the children would orphan. By
    killing children first, even a hard-killed daemon leaves no
    survivors.
    """
    call_order: list[tuple[str, int]] = []

    def fake_find(pid):
        return {29868, 7261}  # two children

    def fake_force_kill(pid):
        call_order.append(("force_kill", pid))

    def fake_os_kill(pid, sig):
        call_order.append(("os_kill", pid))

    # Simulate the daemon dying on the first signal so the polling
    # loop exits cleanly. The first call to _is_process_alive after
    # the kill returns False.
    alive_state = {"alive": True}

    def fake_is_alive(pid):
        # Once we've sent any signal, pretend the daemon dies.
        if any(c[0] in ("os_kill", "force_kill") and c[1] == pid for c in call_order):
            return False
        return alive_state["alive"]

    monkeypatch.setattr(sidecar_pid.os, "kill", fake_os_kill)
    monkeypatch.setattr(sidecar_pid, "_is_process_alive", fake_is_alive)
    monkeypatch.setattr(sidecar_pid, "_record_matches_process", lambda pid: True)

    def fake_start_token(pid):
        if any(c[0] in ("os_kill", "force_kill") and c[1] == pid for c in call_order):
            return None
        return "start-token"

    monkeypatch.setattr(sidecar_pid, "process_start_token", fake_start_token)
    monkeypatch.setattr(sidecar_pid, "_remove_pid_file", lambda: None)
    # Patch the late-bound imports inside takeover_existing_daemon:
    monkeypatch.setattr(process, "find_child_pids", fake_find)
    monkeypatch.setattr(process, "terminate_tree", fake_force_kill)

    result = sidecar_pid.takeover_existing_daemon(7260, wait_seconds=0.5)
    assert result.outcome == "terminated"

    # Children were force-killed before any os.kill on the daemon.
    force_kill_calls = [c for c in call_order if c[0] == "force_kill"]
    daemon_kill_idx = next(
        i for i, c in enumerate(call_order) if c[0] == "os_kill" and c[1] == 7260
    )
    for fkc in force_kill_calls:
        assert call_order.index(fkc) < daemon_kill_idx, (
            "Children must be force-killed BEFORE the daemon — otherwise "
            "a hard-killed daemon orphans them and the new daemon's "
            "port-cleanup is the only remaining defense."
        )
    # Both children were targeted.
    assert {pid for action, pid in force_kill_calls} == {29868, 7261}


def test_takeover_with_no_children_still_kills_daemon(monkeypatch):
    """A daemon with no children → just kill the daemon. No-op on the
    children path; no spurious force-kills."""
    monkeypatch.setattr(process, "find_child_pids", lambda pid: set())
    fk_calls: list[int] = []
    monkeypatch.setattr(process, "terminate_tree", lambda pid: fk_calls.append(pid))
    monkeypatch.setattr(sidecar_pid.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(sidecar_pid, "_is_process_alive", lambda pid: False)
    monkeypatch.setattr(sidecar_pid, "_record_matches_process", lambda pid: True)
    tokens = iter(
        ["start-token", "start-token", "start-token", "start-token", None]
    )
    monkeypatch.setattr(sidecar_pid, "process_start_token", lambda pid: next(tokens))
    monkeypatch.setattr(sidecar_pid, "_remove_pid_file", lambda: None)

    result = sidecar_pid.takeover_existing_daemon(7260, wait_seconds=0.3)
    assert result.outcome == "terminated"
    # No children → no force-kill calls during the children-reap step.
    assert fk_calls == []


def test_takeover_reused_pid_never_enumerates_or_kills(monkeypatch):
    """A live unrelated process behind a stale PID file is never touched."""
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(sidecar_pid, "process_start_token", lambda pid: "start-token")
    monkeypatch.setattr(sidecar_pid, "_record_matches_process", lambda pid: False)
    monkeypatch.setattr(
        process,
        "find_child_pids",
        lambda pid: calls.append(("find_children", pid)) or {999},
    )
    monkeypatch.setattr(
        process,
        "terminate_tree",
        lambda pid: calls.append(("force_kill", pid)),
    )
    monkeypatch.setattr(
        sidecar_pid.os,
        "kill",
        lambda pid, sig: calls.append(("os_kill", pid)),
    )
    monkeypatch.setattr(
        sidecar_pid,
        "_remove_pid_file",
        lambda: calls.append(("remove", 4242)),
    )

    result = sidecar_pid.takeover_existing_daemon(4242, wait_seconds=0.1)
    assert result.outcome == "not_ours"
    assert calls == [("remove", 4242)]


def test_takeover_unverifiable_pid_fails_closed(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(sidecar_pid, "process_start_token", lambda pid: "start-token")
    monkeypatch.setattr(sidecar_pid, "_record_matches_process", lambda pid: None)
    monkeypatch.setattr(
        process,
        "find_child_pids",
        lambda pid: calls.append(("find_children", pid)) or set(),
    )
    monkeypatch.setattr(
        sidecar_pid.os,
        "kill",
        lambda pid, sig: calls.append(("os_kill", pid)),
    )

    result = sidecar_pid.takeover_existing_daemon(4242, wait_seconds=0.1)
    assert result.outcome == "refused"
    assert calls == []


def test_takeover_stops_if_pid_is_reused_during_enumeration(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(sidecar_pid, "_record_matches_process", lambda pid: True)
    tokens = iter(["original", "original", "replacement"])
    monkeypatch.setattr(sidecar_pid, "process_start_token", lambda pid: next(tokens))
    monkeypatch.setattr(
        process,
        "find_child_pids",
        lambda pid: calls.append(("find_children", pid)) or {99},
    )
    monkeypatch.setattr(
        process,
        "terminate_tree",
        lambda pid: calls.append(("force_kill", pid)),
    )
    monkeypatch.setattr(
        sidecar_pid.os,
        "kill",
        lambda pid, sig: calls.append(("os_kill", pid)),
    )
    monkeypatch.setattr(
        sidecar_pid,
        "_remove_pid_file",
        lambda: calls.append(("remove", 4242)),
    )

    result = sidecar_pid.takeover_existing_daemon(4242, wait_seconds=0.1)
    assert result.outcome == "already_gone"
    assert calls == [("find_children", 4242), ("remove", 4242)]


def test_takeover_stops_if_pid_is_reused_during_identity_verification(monkeypatch):
    calls: list[tuple[str, int]] = []
    tokens = iter(["original", "replacement"])
    monkeypatch.setattr(sidecar_pid, "process_start_token", lambda pid: next(tokens))
    monkeypatch.setattr(sidecar_pid, "_record_matches_process", lambda pid: True)
    monkeypatch.setattr(
        process,
        "find_child_pids",
        lambda pid: calls.append(("find_children", pid)) or {99},
    )
    monkeypatch.setattr(
        sidecar_pid.os,
        "kill",
        lambda pid, sig: calls.append(("os_kill", pid)),
    )
    monkeypatch.setattr(
        sidecar_pid,
        "_remove_pid_file",
        lambda: calls.append(("remove", 4242)),
    )

    result = sidecar_pid.takeover_existing_daemon(4242, wait_seconds=0.1)
    assert result.outcome == "already_gone"
    assert calls == [("remove", 4242)]


def test_legacy_process_classifier_requires_sidecar_command():
    assert sidecar_pid._looks_like_sidecar_process(
        "python.exe",
        r'python.exe -m work_buddy.sidecar',
    ) is True
    assert sidecar_pid._looks_like_sidecar_process(
        "wbuddy.exe",
        r'wbuddy.exe start --foreground',
    ) is True
    assert sidecar_pid._looks_like_sidecar_process("ctfmon.exe", "") is False
    assert sidecar_pid._looks_like_sidecar_process(
        "python.exe",
        r'python.exe -m unrelated.worker',
    ) is False
    assert sidecar_pid._looks_like_sidecar_process("python.exe", "") is None
