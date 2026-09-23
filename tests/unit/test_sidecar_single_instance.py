"""The sidecar's single-instance invariant, proven with real processes.

What these tests pin down
-------------------------

Exactly one sidecar may run per data root, and ``stop`` must be able to prove
that it stopped. Each property below, if broken, would let two daemons run at
once, which double-executes every scheduled job and can leave a supervisor
running after a stop reports success:

1. Two processes cannot both hold the instance lock, deleting its file does not
   let a second one in, and the kernel releases it when the holder dies.
   → :class:`TestMutualExclusion`
2. The daemon claims the lock before any other boot work, and exits if it
   loses. → :class:`TestDaemonAdmission`
3. Reading sidecar status never deletes a daemon's registration, however often
   the tray polls. → :class:`TestStatusReadsAreNonDestructive`
4. A PID file that vanished mid-read is a race, not corruption, and does not
   provoke a delete. → :class:`TestAbsentIsNotCorrupt`
5. Takeover reports whether it actually terminated anything.
   → :class:`TestTakeoverHonesty`
6. ``stop`` verifies against the lock and fails loudly if a sidecar survives.
   → :class:`TestStopVerifies`
7. A live daemon with no PID file is never classified "down", so ``start`` never
   spawns beside it. → :class:`TestHealthDoesNotInviteADuplicate`
8. A duplicate is visible if one ever escapes the lock.
   → :class:`TestDetectability`

Why these start real processes
------------------------------

"Two sidecars cannot coexist" is a property of the operating system's locking,
not of work-buddy's control flow, and a mocked lock proves only that the mock
was configured to return False. Every exclusion test here spawns a genuine
second Python process that calls the real acquisition code against a real file.

What they deliberately do NOT start is the full daemon. Bringing up
``python -m work_buddy.sidecar`` twice would spawn ten child services and hold
~2.5 GB, and would bind the live service ports (5123-5127) out from under a
running installation. :class:`TestDaemonAdmission` instead drives
``daemon.admit_single_instance``, the real admission function that the real
``run()`` calls as its first statement, in a real subprocess. The boundary between "what
the OS guarantees" and "what the daemon wires to it" is therefore covered on
both sides without the service stack.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from work_buddy.sidecar import instance_lock, pid as sidecar_pid


REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def lock_dir(tmp_path, monkeypatch):
    """Redirect the instance lock and PID file into a temp directory.

    Both are module-level ``Path`` constants resolved at import, so the
    redirection is a monkeypatch of the constants rather than of ``paths``.
    ``_identity_file`` and ``_owner_file`` derive from them, so they follow.
    """
    monkeypatch.setattr(instance_lock, "LOCK_FILE", tmp_path / "sidecar.lock")
    monkeypatch.setattr(sidecar_pid, "PID_FILE", tmp_path / "sidecar.pid")
    monkeypatch.setattr(instance_lock, "_held", None)
    yield tmp_path
    instance_lock.release()


def _run_child(body: str, *, lock_path: Path, pid_path: Path, timeout: float = 60.0):
    """Execute ``body`` in a real child process with the paths redirected.

    The child imports the genuine modules and prints a single JSON line. Its
    exit code and that line are the assertions' evidence. ``-u`` keeps the
    output unbuffered so a child that we later kill has still flushed.
    """
    script = textwrap.dedent(
        f"""
        import json, os, sys, time
        from pathlib import Path
        os.environ.setdefault("WORK_BUDDY_SESSION_ID", "test-{os.getpid()}")
        from work_buddy.sidecar import instance_lock
        from work_buddy.sidecar import pid as sidecar_pid
        instance_lock.LOCK_FILE = Path(r"{lock_path}")
        sidecar_pid.PID_FILE = Path(r"{pid_path}")
        """
    ) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-u", "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _spawn_holder(lock_path: Path, pid_path: Path, *, ready_file: Path):
    """Start a child that acquires the lock and then blocks, holding it.

    Returns the Popen once the child has signalled readiness by creating
    ``ready_file``. The child is a real process holding a real lock; the tests
    that follow contend against it exactly as a second sidecar would.
    """
    script = textwrap.dedent(
        f"""
        import os, sys, time
        from pathlib import Path
        os.environ.setdefault("WORK_BUDDY_SESSION_ID", "holder")
        from work_buddy.sidecar import instance_lock
        from work_buddy.sidecar import pid as sidecar_pid
        instance_lock.LOCK_FILE = Path(r"{lock_path}")
        sidecar_pid.PID_FILE = Path(r"{pid_path}")
        instance_lock.acquire()
        sidecar_pid.write_pid_file()
        # Report our OWN pid, not the one Popen saw. On Windows a uv/virtualenv
        # python.exe is a trampoline that re-execs the real interpreter, so
        # Popen.pid names the trampoline and os.getpid() names the process that
        # actually holds the lock.
        Path(r"{ready_file}").write_text(str(os.getpid()))
        # Hold the lock open. The test kills this process; the kernel is what
        # releases the lock, which is the property under test.
        time.sleep(600)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if ready_file.exists():
            try:
                proc.real_pid = int(ready_file.read_text().strip())
            except (ValueError, OSError):
                time.sleep(0.05)
                continue
            return proc
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(f"lock holder exited early: rc={proc.returncode}\n{err}\n{out}")
        time.sleep(0.1)
    proc.kill()
    pytest.fail("lock holder never signalled readiness")


# ---------------------------------------------------------------------------
# 1. The property: two holders cannot coexist
# ---------------------------------------------------------------------------

class TestMutualExclusion:
    """Exclusion is enforced by the OS, across real process boundaries."""

    def test_second_process_cannot_acquire_a_held_lock(self, lock_dir):
        """The core invariant: a second process is refused while the first holds the lock."""
        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        try:
            result = _run_child(
                """
                try:
                    instance_lock.acquire()
                    print(json.dumps({"acquired": True}))
                except instance_lock.InstanceLockHeld as exc:
                    print(json.dumps({"acquired": False, "owner": exc.owner}))
                """,
                lock_path=lock_dir / "sidecar.lock",
                pid_path=lock_dir / "sidecar.pid",
            )
            assert result.returncode == 0, result.stderr
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            assert payload["acquired"] is False, (
                "a second process acquired a lock another process holds, so "
                "mutual exclusion is not enforced"
            )
            # Owner metadata is informational, but it should name the holder so
            # the operator message is actionable.
            assert payload["owner"].get("pid") == holder.real_pid
        finally:
            holder.kill()
            holder.wait(timeout=30)

    def test_kernel_releases_the_lock_when_the_holder_is_hard_killed(self, lock_dir):
        """No cleanup code runs on a hard kill, so the kernel must do it.

        This is why the invariant rests on a lock rather than the PID file:
        ``TerminateProcess``
        skips ``atexit``, signal handlers and ``finally`` blocks, so any
        file-based record can outlive its owner. A lock cannot.
        """
        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        holder.kill()
        holder.wait(timeout=30)

        deadline = time.monotonic() + 15
        while instance_lock.is_locked() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not instance_lock.is_locked(), (
            "the lock survived its holder's death, so a future sidecar could "
            "never start"
        )
        # And the successor can take it.
        instance_lock.acquire().release()

    def test_deleting_the_lock_file_does_not_let_a_second_instance_in(
        self, lock_dir
    ):
        """Deleting the lock file must not be a way to defeat the invariant.

        The mechanism differs by platform and the difference is real, not
        cosmetic:

        - **Windows**: the open handle makes the unlink fail outright.
        - **Linux**: the unlink succeeds and the newcomer would create a *fresh
          inode* and lock that, so the file lock alone would let it in. The
          abstract-namespace name closes it, since that lives in the kernel
          with no directory entry to delete.
        - **Other POSIX** (macOS, BSD): no abstract namespace, so the file lock
          stands alone and this is a known residual exposure. Asserted
          explicitly rather than skipped, so the limitation stays visible.
        """
        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        try:
            try:
                (lock_dir / "sidecar.lock").unlink()
            except OSError:
                pass  # Windows: refused because the handle is open. Fine.

            result = _run_child(
                """
                try:
                    instance_lock.acquire()
                    print(json.dumps({"acquired": True}))
                except (instance_lock.InstanceLockHeld,
                        instance_lock.InstanceLockUnavailable) as exc:
                    print(json.dumps({"acquired": False, "why": type(exc).__name__}))
                """,
                lock_path=lock_dir / "sidecar.lock",
                pid_path=lock_dir / "sidecar.pid",
            )
            assert result.returncode == 0, result.stderr
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            if sys.platform == "win32" or sys.platform.startswith("linux"):
                assert payload["acquired"] is False, (
                    "deleting the lock file let a second sidecar in"
                )
            else:
                # Documented gap, not a passing guarantee. If this ever starts
                # refusing on macOS/BSD, a stronger primitive landed and this
                # branch should become an assertion of the strong behaviour.
                assert payload["acquired"] is True, (
                    "unexpected refusal: a platform without an abstract "
                    "namespace should exhibit the documented file-lock gap"
                )
        finally:
            holder.kill()
            holder.wait(timeout=30)

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="abstract AF_UNIX namespace is Linux-only",
    )
    def test_abstract_name_is_per_data_root(self, lock_dir, tmp_path):
        """Two data roots must not exclude each other.

        work-buddy is multi-user, and a single global abstract name would make
        a second account's or a second checkout's sidecar refuse to start.
        """
        first = instance_lock._abstract_name()
        monkey = tmp_path / "other" / "sidecar.lock"
        monkey.parent.mkdir(parents=True, exist_ok=True)
        original = instance_lock.LOCK_FILE
        try:
            instance_lock.LOCK_FILE = monkey
            second = instance_lock._abstract_name()
        finally:
            instance_lock.LOCK_FILE = original
        assert first and second and first != second

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="abstract AF_UNIX namespace is Linux-only",
    )
    def test_abstract_name_is_released_when_the_holder_dies(self, lock_dir):
        """The kernel must drop the name, or no successor could ever start."""
        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        assert instance_lock._bind_abstract() is False
        holder.kill()
        holder.wait(timeout=30)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            probe = instance_lock._bind_abstract()
            if probe not in (False, None):
                probe.close()
                return
            time.sleep(0.1)
        pytest.fail("the abstract name survived its holder's death")

    def test_is_locked_is_false_when_nothing_holds_it(self, lock_dir):
        assert instance_lock.is_locked() is False

    def test_acquire_is_idempotent_within_one_process(self, lock_dir):
        """A double ``acquire`` must not deadlock against itself or leak a fd."""
        first = instance_lock.acquire()
        second = instance_lock.acquire()
        assert first is second


# ---------------------------------------------------------------------------
# 2. The daemon wires the lock to its boot
# ---------------------------------------------------------------------------

class TestDaemonAdmission:
    """``daemon.admit_single_instance`` is the real admission path."""

    def test_admission_exits_when_another_instance_holds_the_lock(self, lock_dir):
        """A second daemon exits with the already-running code instead of booting.

        Admission consults the lock, not the PID file, so a registration that a
        status poll or anything else removed cannot let a second daemon through.
        """
        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        try:
            result = _run_child(
                """
                from work_buddy.sidecar import daemon
                try:
                    daemon.admit_single_instance()
                except SystemExit as exc:
                    print(json.dumps({"exit": exc.code}))
                    sys.exit(0)
                print(json.dumps({"exit": None, "admitted": True}))
                """,
                lock_path=lock_dir / "sidecar.lock",
                pid_path=lock_dir / "sidecar.pid",
                timeout=180,
            )
            assert result.returncode == 0, result.stderr
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            assert payload["exit"] == 3, (
                "the daemon admitted a second instance while the first held "
                f"the lock (payload={payload})"
            )
        finally:
            holder.kill()
            holder.wait(timeout=30)

    def test_admission_runs_before_any_other_boot_work(self):
        """The gate's *position* matters as much as its existence.

        Everything that runs before admission is a window in which a second
        launcher can also decide it is the only sidecar, and config loading and
        the bootstraps take real time. A gate placed late is a gate two
        launchers walk through, so this asserts the call is the first effectful
        statement of ``run()`` and fails if work is ever inserted above it.
        """
        import ast
        import inspect

        from work_buddy.sidecar import daemon

        tree = ast.parse(textwrap.dedent(inspect.getsource(daemon.run)))
        body = tree.body[0].body
        effectful = [
            node for node in body
            if not isinstance(node, (ast.Expr, ast.Global))
            or (isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant))
        ]
        first = effectful[0]
        assert isinstance(first, ast.Expr), f"unexpected first statement: {ast.dump(first)}"
        assert isinstance(first.value, ast.Call)
        assert getattr(first.value.func, "id", None) == "admit_single_instance", (
            "admit_single_instance() is not the first effectful statement of "
            "daemon.run(); anything above it opens a window in which two "
            "sidecars can both start"
        )


# ---------------------------------------------------------------------------
# 3. Status reads must not mutate
# ---------------------------------------------------------------------------

class TestStatusReadsAreNonDestructive:
    """Reading status must never delete a daemon's registration."""

    def test_check_existing_daemon_never_removes_a_pid_file(self, lock_dir):
        """A live pid that is provably not a sidecar is reported, not deleted.

        Uses a live pid (this test process) recorded with no identity companion,
        so ``_record_matches_process`` takes the legacy path and proves the
        process is *not* a sidecar. Removing the record in response is
        reconcile's decision, under the lock, never a reader's.
        """
        sidecar_pid.PID_FILE.write_text(f"{os.getpid()}\n")
        assert sidecar_pid.check_existing_daemon() is None  # not a sidecar
        assert sidecar_pid.PID_FILE.exists(), (
            "a read-only liveness check deleted the PID file"
        )

    def test_check_existing_daemon_leaves_a_dead_pid_record_alone(self, lock_dir):
        sidecar_pid.PID_FILE.write_text("999999\n")
        assert sidecar_pid.check_existing_daemon() is None
        assert sidecar_pid.PID_FILE.exists(), (
            "a read-only liveness check deleted a stale PID file; cleanup "
            "belongs to reconcile_pid_file, under the instance lock"
        )

    def test_tray_status_poll_does_not_delete_a_live_daemons_registration(
        self, lock_dir, monkeypatch
    ):
        """The tray's status poll leaves a live daemon's PID file in place.

        The tray calls ``tray.status.read_status()`` every 2.5 s, so any
        mutation on that path races a booting daemon writing its record.
        Several polls against a real lock holder must leave its PID file
        untouched.
        """
        from work_buddy.tray import status as tray_status

        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        try:
            assert sidecar_pid.PID_FILE.exists()
            for _ in range(5):  # several poll ticks, as the tray would
                tray_status.read_status()
            assert sidecar_pid.PID_FILE.exists(), (
                "the tray's status poll deleted the running daemon's PID file"
            )
            assert instance_lock.is_locked(), "the holder should still be running"
        finally:
            holder.kill()
            holder.wait(timeout=30)

    def test_reconcile_is_the_only_remover(self, lock_dir):
        """Cleanup happens through one explicit caller."""
        sidecar_pid.PID_FILE.write_text("999999\n")
        assert sidecar_pid.reconcile_pid_file() == "dead"
        assert not sidecar_pid.PID_FILE.exists()


# ---------------------------------------------------------------------------
# 4. Absent is not corrupt
# ---------------------------------------------------------------------------

class TestAbsentIsNotCorrupt:
    """A file that vanished is a race, not damage."""

    def test_missing_file_reads_as_absent(self, lock_dir):
        assert sidecar_pid._read_pid_record() == ("absent", None)

    def test_unparseable_file_reads_as_corrupt(self, lock_dir):
        sidecar_pid.PID_FILE.write_text("not-a-pid\n")
        state, value = sidecar_pid._read_pid_record()
        assert (state, value) == ("corrupt", None)

    def test_reconcile_does_not_treat_absent_as_corrupt(self, lock_dir):
        """An absent file is not treated as corrupt, so nothing is removed.

        Treating "could not read it" as "corrupt" is what turns a race with a
        writer into the deletion of a live daemon's registration.
        """
        assert sidecar_pid.reconcile_pid_file() is None

    def test_reconcile_removes_the_identity_companion_with_a_corrupt_file(
        self, lock_dir
    ):
        sidecar_pid.PID_FILE.write_text("garbage")
        sidecar_pid._identity_file().write_text("{}")
        assert sidecar_pid.reconcile_pid_file() == "corrupt"
        assert not sidecar_pid.PID_FILE.exists()
        assert not sidecar_pid._identity_file().exists()


# ---------------------------------------------------------------------------
# 5. Takeover tells the truth about what it did
# ---------------------------------------------------------------------------

class TestTakeoverHonesty:
    """Takeover distinguishes "safe to proceed" from "a sidecar was terminated"."""

    def test_not_ours_is_safe_to_proceed_but_not_terminated(self, lock_dir):
        """A live non-sidecar pid: proceeding is fine, but nothing was stopped.

        A bare ``True`` here would let ``stop_sidecar`` report *"Sidecar
        stopped."* after stopping nothing.
        """
        result = sidecar_pid.takeover_existing_daemon(os.getpid(), wait_seconds=0.1)
        assert bool(result) is True
        assert result.outcome == "not_ours"
        assert result.terminated is False

    def test_refused_is_falsy(self, lock_dir, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.sidecar.pid.process_start_token", lambda pid: "tok"
        )
        monkeypatch.setattr(
            "work_buddy.sidecar.pid._record_matches_process", lambda pid: None
        )
        result = sidecar_pid.takeover_existing_daemon(4242, wait_seconds=0.1)
        assert bool(result) is False
        assert result.outcome == "refused"


# ---------------------------------------------------------------------------
# 6. Stop verifies, and says so when it cannot
# ---------------------------------------------------------------------------

class TestStopVerifies:
    """``stop`` must stop work-buddy, and prove it."""

    def test_stop_reports_failure_when_a_sidecar_survives(self, lock_dir, monkeypatch):
        """Stop fails loudly when the pid it terminated was not the lock holder.

        When the PID file names one daemon and a different one holds the lock,
        terminating the named one leaves work-buddy running. Stop must consult
        the lock afterwards and refuse to claim success.
        """
        from work_buddy.cli import lifecycle

        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        try:
            # The PID file names a different daemon from the one holding the
            # lock.
            monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: 4242)
            monkeypatch.setattr(
                lifecycle._pid,
                "takeover_existing_daemon",
                lambda pid, **kw: sidecar_pid.TakeoverResult(True, "terminated"),
            )
            monkeypatch.setattr(lifecycle, "_STOP_VERIFY_S", 1.0)

            result = lifecycle.stop_sidecar()
            assert result["stopped"] is False, (
                "stop reported success while a sidecar still held the lock"
            )
            assert "STILL RUNNING" in result["detail"]
        finally:
            holder.kill()
            holder.wait(timeout=30)

    def test_stop_reports_not_running_only_when_the_lock_is_free(
        self, lock_dir, monkeypatch
    ):
        from work_buddy.cli import lifecycle

        monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: None)
        result = lifecycle.stop_sidecar()
        assert result == {
            "stopped": False,
            "was_running": False,
            "pid": None,
            "detail": "Sidecar not running.",
        }

    def test_stop_refuses_when_the_lock_is_held_but_unidentified(
        self, lock_dir, monkeypatch
    ):
        """No PID file plus a held lock means a sidecar is running that stop
        cannot identify. ``stop`` must not call that "not running"."""
        from work_buddy.cli import lifecycle

        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        try:
            monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: None)
            result = lifecycle.stop_sidecar()
            assert result["was_running"] is True
            assert result["stopped"] is False
            assert "instance lock" in result["detail"]
        finally:
            holder.kill()
            holder.wait(timeout=30)


# ---------------------------------------------------------------------------
# 7. A live daemon with no PID file is never classified "down"
# ---------------------------------------------------------------------------

class TestHealthDoesNotInviteADuplicate:
    """Health must never classify a live, locked daemon so that ``start`` spawns beside it."""

    def test_held_lock_without_a_pid_file_is_not_down(self, lock_dir):
        from work_buddy.cli import lifecycle

        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        try:
            sidecar_pid.PID_FILE.unlink(missing_ok=True)
            health = lifecycle.daemon_health(None, None)
            assert health != "down", (
                "a live daemon holding the lock was classified 'down'; "
                "'wbuddy start' would spawn a second one"
            )
        finally:
            holder.kill()
            holder.wait(timeout=30)

    def test_start_refuses_to_spawn_alongside_a_wedged_daemon(
        self, lock_dir, monkeypatch
    ):
        """A wedged daemon that holds the lock is reported, not joined by a second.

        Replacement is ``restart``'s job: it stops the incumbent and verifies
        that before launching a successor.
        """
        from work_buddy.cli import lifecycle

        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        spawned: list[object] = []
        try:
            monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: 4242)
            monkeypatch.setattr(lifecycle, "_daemon_health", lambda *a: "wedged")
            monkeypatch.setattr(
                lifecycle.subprocess,
                "Popen",
                lambda *a, **kw: spawned.append(a) or (_ for _ in ()).throw(
                    AssertionError("start spawned a second sidecar")
                ),
            )
            result = lifecycle.start_sidecar(wait_seconds=0.1)
            assert result["started"] is False
            assert result["already_running"] is True
            assert spawned == []
        finally:
            holder.kill()
            holder.wait(timeout=30)


    def test_a_held_lock_after_spawn_reports_running_not_failure(
        self, lock_dir, monkeypatch
    ):
        """A spawn whose PID file has not confirmed, while the lock is held,
        is reported as running.

        ``start`` only spawns when the lock was free, so a lock held afterwards
        is most likely the daemon it just launched. Calling that a failure
        would invite the operator to launch again.
        """
        from work_buddy.cli import lifecycle

        ready = lock_dir / "ready"
        holder = _spawn_holder(
            lock_dir / "sidecar.lock", lock_dir / "sidecar.pid", ready_file=ready
        )
        try:
            # Force the "down" classification so start proceeds to the spawn,
            # and make the spawn a no-op: the holder stands in for the daemon
            # it launched.
            monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: None)
            monkeypatch.setattr(lifecycle, "_daemon_health", lambda *a: "down")
            monkeypatch.setattr(
                lifecycle.subprocess, "Popen", lambda *a, **kw: None
            )
            result = lifecycle.start_sidecar(wait_seconds=0.5)
            assert result["started"] is True
            assert result["already_running"] is False
            assert "holds the instance lock" in result["detail"]
            assert result["pid"] == holder.real_pid
        finally:
            holder.kill()
            holder.wait(timeout=30)

    def test_a_free_lock_after_spawn_is_a_genuine_failure(
        self, lock_dir, monkeypatch
    ):
        """If nothing holds the lock after the wait, the launched daemon died."""
        from work_buddy.cli import lifecycle

        monkeypatch.setattr(lifecycle._pid, "check_existing_daemon", lambda: None)
        monkeypatch.setattr(lifecycle, "_daemon_health", lambda *a: "down")
        monkeypatch.setattr(lifecycle.subprocess, "Popen", lambda *a, **kw: None)
        result = lifecycle.start_sidecar(wait_seconds=0.5)
        assert result["started"] is False
        assert result["already_running"] is False
        assert "nothing holds the instance lock" in result["detail"]


# ---------------------------------------------------------------------------
# 8. Detectability: a duplicate is visible if it happens anyway
# ---------------------------------------------------------------------------

class TestDetectability:
    """A duplicate must be visible if one ever escapes the lock."""

    def test_log_lines_carry_the_writing_process_pid(self):
        """Every sidecar instance shares one log file, because
        ``session_id[:8]`` of ``sidecar-<8 hex>`` is the constant ``"sidecar-"``.
        The pid on each line is what makes two writers legible.
        """
        import logging

        from work_buddy import logging_config

        # Read the configured format rather than a live log, so the assertion
        # holds in a test process that configures logging differently.
        source = Path(logging_config.__file__).read_text(encoding="utf-8")
        assert "%(process)" in source, (
            "the file log format does not identify the writing process, so "
            "concurrent writers are indistinguishable"
        )
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | pid=%(process)-6d | %(name)s | %(message)s"
        )
        record = logging.LogRecord(
            "work_buddy.test", logging.INFO, __file__, 1, "hello", None, None
        )
        assert f"pid={os.getpid()}" in formatter.format(record)

    def test_find_sidecar_processes_excludes_the_caller(self):
        """The boot-time duplicate scan must not report the booting daemon."""
        found = instance_lock.find_sidecar_processes()
        assert os.getpid() not in found

    def test_the_process_table_actually_parses(self):
        """A scan that silently parses nothing is worse than no scan at all.

        A quoting slip in the PowerShell query (a backtick escape inside a
        single-quoted string is literal) would make every row fail to split,
        and the duplicate alarm could then never fire on any machine. Asserting
        that this process finds itself in the table is what makes that failure
        loud.
        """
        rows = instance_lock._process_table()
        assert rows, "the process table came back empty; the query or the parse is broken"
        pids = {pid for pid, _ppid, _cmd in rows}
        assert os.getpid() in pids, (
            "this Python process is not in the table it just enumerated, so "
            "the rows are not being parsed correctly"
        )
        assert any(cmd.strip() for _pid, _ppid, cmd in rows), (
            "no row carried a command line, so the duplicate scan can never match"
        )
