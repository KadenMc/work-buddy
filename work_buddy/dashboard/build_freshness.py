"""Boot-time freshness guard for the development React dashboard build.

``dashboard-react/dist`` is gitignored in a source checkout, but Flask serves
it directly.  The sidecar calls :func:`ensure_dashboard_react_build` once per
boot before launching the dashboard child.  Builds are produced in a sibling
staging directory and swapped into place only after validation, so a failed or
racing build never destroys the last-good payload.

Packaged installations do not include the complete React development tree.
They take a fast path that validates and trusts the shipped ``dist`` payload;
Node.js is not a runtime dependency for those installations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator

from work_buddy import paths
from work_buddy.compat import subprocess_creation_flags


_MARKER_VERSION = 1
_BUILD_MARKER_NAME = ".work-buddy-build.json"
_ERROR_MARKER_NAME = ".work-buddy-build-error.json"
_LOCK_NAME = ".work-buddy-build.lock"
_BUILD_STATE_ENV = "WORK_BUDDY_DASHBOARD_REACT_BUILD_STATE"
_READY_BUILD_STATE = "ready"
_DEFAULT_BUILD_TIMEOUT_SECONDS = 180.0
_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
_DEFAULT_LOCK_TIMEOUT_SECONDS = 180.0
_STAGING_PREFIX = ".work-buddy-dist-staging-"
_BACKUP_PREFIX = ".work-buddy-dist-backup-"
_REQUIRED_DEVELOPMENT_INPUTS = (
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
    "index.html",
)
_BUILD_ENV_FILES = (
    ".env",
    ".env.local",
    ".env.production",
    ".env.production.local",
)


class DashboardBuildStatus(str, Enum):
    """Typed outcome of the React dashboard build preflight."""

    PACKAGED_DIST = "packaged_dist"
    CURRENT_MARKER = "current_marker"
    BUILT = "built"
    MISSING_PACKAGED_DIST = "missing_packaged_dist"
    INPUT_SCAN_FAILED = "input_scan_failed"
    INCOMPLETE_DEVELOPMENT_CHECKOUT = "incomplete_development_checkout"
    NPM_UNAVAILABLE = "npm_unavailable"
    BUILD_LOCK_TIMED_OUT = "build_lock_timed_out"
    DASHBOARD_PORT_BUSY = "dashboard_port_busy"
    INTERNAL_ERROR = "internal_error"
    BUILD_TIMED_OUT = "build_timed_out"
    BUILD_CANCELLED = "build_cancelled"
    BUILD_FAILED = "build_failed"
    INVALID_BUILD_OUTPUT = "invalid_build_output"
    INPUTS_CHANGED = "inputs_changed"
    MARKER_WRITE_FAILED = "marker_write_failed"
    BUILD_SWAP_FAILED = "build_swap_failed"


_READY_STATUSES = frozenset(
    {
        DashboardBuildStatus.PACKAGED_DIST,
        DashboardBuildStatus.CURRENT_MARKER,
        DashboardBuildStatus.BUILT,
    }
)


@dataclass(frozen=True)
class DashboardBuildResult:
    """Result returned to the sidecar's dashboard-launch boundary."""

    status: DashboardBuildStatus
    message: str
    dashboard_root: Path
    dist_root: Path
    input_fingerprint: str | None = None
    elapsed_seconds: float = 0.0
    returncode: int | None = None
    diagnostic: str | None = None

    @property
    def ready(self) -> bool:
        return self.status in _READY_STATUSES


@dataclass(frozen=True)
class DashboardBuildError:
    """Failure exposed by Flask as a clear React-app 503."""

    status: str
    message: str
    recorded_at: float


@dataclass(frozen=True)
class _InputSnapshot:
    fingerprint: str


@dataclass(frozen=True)
class _OutputSnapshot:
    fingerprint: str


class _BuildLockTimeout(TimeoutError):
    pass


class _BuildCancelled(RuntimeError):
    pass


def _dashboard_root() -> Path:
    return paths.asset_root() / "dashboard-react"


def dashboard_build_error_marker_path() -> Path:
    """Return the marker Flask consults before serving the React app."""

    return _dashboard_root() / _ERROR_MARKER_NAME


def read_dashboard_build_error() -> DashboardBuildError | None:
    """Return the sidecar-authoritative build error, when one exists.

    The sidecar passes the result to the dashboard child through an
    environment variable.  That in-memory contract is authoritative for a
    supervised dashboard launch, so a filesystem permission problem cannot
    make failure fail open or make a successfully rebuilt app remain stuck on
    an old error marker.  The marker remains a useful fallback for developers
    who launch Flask directly.
    """

    supervised_state = os.environ.get(_BUILD_STATE_ENV, "").strip()
    if supervised_state == _READY_BUILD_STATE:
        return None
    if supervised_state:
        return DashboardBuildError(
            supervised_state,
            "The React dashboard could not be prepared from the current "
            "source checkout. See the sidecar log for the build details.",
            0.0,
        )

    try:
        payload = json.loads(
            dashboard_build_error_marker_path().read_text(encoding="utf-8")
        )
        status = payload["status"]
        message = payload["message"]
        recorded_at = float(payload["recorded_at"])
        if not isinstance(status, str) or not isinstance(message, str):
            return None
        return DashboardBuildError(status, message, recorded_at)
    except (
        KeyError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None


def dashboard_build_state_environment(result: DashboardBuildResult | None) -> str:
    """Return the value the sidecar must pass to its dashboard child."""

    if result is not None and result.ready:
        return _READY_BUILD_STATE
    if result is None:
        return DashboardBuildStatus.INTERNAL_ERROR.value
    return result.status.value


def dashboard_build_environment(
    result: DashboardBuildResult | None,
) -> dict[str, str]:
    """Return the authoritative environment for a supervised Flask child."""

    return {_BUILD_STATE_ENV: dashboard_build_state_environment(result)}


def _development_checkout_problem(dashboard_root: Path) -> str | None:
    """Describe an incomplete source checkout, or return ``None``.

    Packaged payloads intentionally contain only ``dashboard-react/dist``.
    Once ``src`` exists, however, this is an authoring checkout and missing
    build inputs must fail closed instead of silently trusting an old bundle.
    """

    src = dashboard_root / "src"
    authoring_tree_present = src.exists() or any(
        (dashboard_root / name).exists()
        for name in _REQUIRED_DEVELOPMENT_INPUTS
    )
    if not authoring_tree_present:
        return None
    missing = [
        name for name in _REQUIRED_DEVELOPMENT_INPUTS
        if not (dashboard_root / name).is_file()
    ]
    if not src.is_dir():
        missing.insert(0, "src/")
    if missing:
        return "missing required React build inputs: " + ", ".join(missing)
    return ""


def _input_files(dashboard_root: Path) -> Iterable[Path]:
    discovered: set[Path] = set()
    for tree_name in ("src", "public"):
        tree = dashboard_root / tree_name
        if tree.is_dir():
            discovered.update(path for path in tree.rglob("*") if path.is_file())

    root_candidates: set[Path] = {dashboard_root / "index.html"}
    for pattern in ("package*.json", "tsconfig*.json", "vite.config.*"):
        root_candidates.update(dashboard_root.glob(pattern))
    root_candidates.update(dashboard_root / name for name in _BUILD_ENV_FILES)
    discovered.update(path for path in root_candidates if path.is_file())

    yield from sorted(
        discovered,
        key=lambda path: path.relative_to(dashboard_root).as_posix(),
    )


def _snapshot_inputs(dashboard_root: Path) -> _InputSnapshot:
    digest = hashlib.sha256()
    for path in _input_files(dashboard_root):
        relative = path.relative_to(dashboard_root).as_posix()
        stat_before = path.stat()
        file_digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(chunk)
        stat_after = path.stat()
        if (
            stat_before.st_mtime_ns != stat_after.st_mtime_ns
            or stat_before.st_size != stat_after.st_size
        ):
            raise OSError(f"build input changed while it was read: {relative}")

        encoded_relative = relative.encode("utf-8")
        digest.update(len(encoded_relative).to_bytes(8, "big"))
        digest.update(encoded_relative)
        digest.update(file_digest.digest())
    return _InputSnapshot(digest.hexdigest())


def _snapshot_output(dist_root: Path) -> _OutputSnapshot:
    """Fingerprint every emitted file except this guard's own marker."""

    digest = hashlib.sha256()
    files = sorted(
        (
            path for path in dist_root.rglob("*")
            if path.is_file() and path.name != _BUILD_MARKER_NAME
        ),
        key=lambda path: path.relative_to(dist_root).as_posix(),
    )
    for path in files:
        relative = path.relative_to(dist_root).as_posix()
        encoded_relative = relative.encode("utf-8")
        digest.update(len(encoded_relative).to_bytes(8, "big"))
        digest.update(encoded_relative)
        file_digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(chunk)
        digest.update(file_digest.digest())
    return _OutputSnapshot(digest.hexdigest())


def _read_valid_index(index_path: Path) -> bytes | None:
    try:
        if not index_path.is_file():
            return None
        content = index_path.read_bytes()
        if not content.strip():
            return None
        lowered = content[:4096].lower()
        if b"<html" not in lowered and b"<!doctype html" not in lowered:
            return None
    except OSError:
        return None
    return content


_INDEX_LOCAL_REFERENCE_RE = re.compile(
    rb"(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE,
)


def _validate_dist_payload(dist_root: Path) -> _OutputSnapshot | None:
    """Validate the shell and every local file it directly references."""

    index_path = dist_root / "index.html"
    index_bytes = _read_valid_index(index_path)
    if index_bytes is None:
        return None
    try:
        for raw_reference in _INDEX_LOCAL_REFERENCE_RE.findall(index_bytes):
            reference = raw_reference.decode("utf-8", errors="strict")
            reference = reference.split("#", 1)[0].split("?", 1)[0]
            if not reference or reference.startswith(("http:", "https:", "data:", "//")):
                continue
            if reference.startswith("/app/"):
                relative = reference[len("/app/"):]
            elif reference.startswith("/"):
                # References outside the React mount are Flask-owned, not
                # files from this Vite payload.
                continue
            else:
                relative = reference[2:] if reference.startswith("./") else reference
            relative_parts = PurePosixPath(relative).parts
            if not relative or ".." in relative_parts:
                return None
            target = dist_root.joinpath(*relative_parts)
            if not target.is_file():
                return None
        return _snapshot_output(dist_root)
    except (OSError, UnicodeDecodeError):
        return None


def _load_marker(marker_path: Path) -> dict[str, Any] | None:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return marker if isinstance(marker, dict) else None


def _marker_matches(
    marker: dict[str, Any] | None,
    snapshot: _InputSnapshot,
    output: _OutputSnapshot,
) -> bool:
    return marker is not None and (
        marker.get("version") == _MARKER_VERSION
        and marker.get("input_fingerprint") == snapshot.fingerprint
        and marker.get("output_fingerprint") == output.fingerprint
    )


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _write_build_marker(
    marker_path: Path,
    snapshot: _InputSnapshot,
    output: _OutputSnapshot,
) -> None:
    _atomic_json_write(marker_path, {
        "version": _MARKER_VERSION,
        "input_fingerprint": snapshot.fingerprint,
        "output_fingerprint": output.fingerprint,
        "recorded_at": time.time(),
    })


def _clear_error_marker(dashboard_root: Path) -> bool:
    try:
        (dashboard_root / _ERROR_MARKER_NAME).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _result(
    status: DashboardBuildStatus,
    message: str,
    dashboard_root: Path,
    started_at: float,
    *,
    fingerprint: str | None = None,
    returncode: int | None = None,
    diagnostic: str | None = None,
) -> DashboardBuildResult:
    return DashboardBuildResult(
        status=status,
        message=message,
        dashboard_root=dashboard_root,
        dist_root=dashboard_root / "dist",
        input_fingerprint=fingerprint,
        elapsed_seconds=max(0.0, time.monotonic() - started_at),
        returncode=returncode,
        diagnostic=diagnostic,
    )


def record_dashboard_build_error(
    status: DashboardBuildStatus,
    message: str,
    *,
    dashboard_root: Path | None = None,
) -> None:
    """Persist a build failure for the React app's 503 response."""

    root = dashboard_root or _dashboard_root()
    _atomic_json_write(root / _ERROR_MARKER_NAME, {
        "version": _MARKER_VERSION,
        "status": status.value,
        "message": message,
        "recorded_at": time.time(),
    })


def _failure(
    status: DashboardBuildStatus,
    message: str,
    dashboard_root: Path,
    started_at: float,
    *,
    fingerprint: str | None = None,
    returncode: int | None = None,
    diagnostic: str | None = None,
) -> DashboardBuildResult:
    try:
        record_dashboard_build_error(status, message, dashboard_root=dashboard_root)
    except OSError as exc:
        message = f"{message} (Could not persist the build-error marker: {exc})"
    return _result(
        status, message, dashboard_root, started_at,
        fingerprint=fingerprint, returncode=returncode,
        diagnostic=diagnostic,
    )


def _npm_executable() -> str | None:
    candidates = ("npm.cmd", "npm") if os.name == "nt" else ("npm", "npm.cmd")
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _output_excerpt(completed: subprocess.CompletedProcess[str]) -> str:
    combined = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    return combined[-4000:] if combined else "no build output was captured"


def _safe_remove_generated_tree(path: Path, dashboard_root: Path) -> None:
    """Remove only a staging/backup directory generated by this module."""

    if path.parent != dashboard_root or not path.name.startswith(
        (_STAGING_PREFIX, _BACKUP_PREFIX)
    ):
        raise ValueError(f"refusing to remove unexpected dashboard path: {path}")
    shutil.rmtree(path, ignore_errors=True)


def _recover_generated_trees(dashboard_root: Path, dist_root: Path) -> None:
    """Recover a last-good dist after an interrupted two-rename swap."""

    stagings = sorted(dashboard_root.glob(f"{_STAGING_PREFIX}*"))
    backups = sorted(
        dashboard_root.glob(f"{_BACKUP_PREFIX}*"),
        key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
        reverse=True,
    )
    if not dist_root.exists():
        restore = next(
            (path for path in backups if _validate_dist_payload(path) is not None),
            None,
        )
        if restore is not None:
            restore.replace(dist_root)
            backups.remove(restore)
    for generated in [*stagings, *backups]:
        if generated.exists():
            _safe_remove_generated_tree(generated, dashboard_root)


@contextmanager
def _dashboard_build_lock(
    dashboard_root: Path,
    *,
    timeout_seconds: float,
    heartbeat: Callable[[], None] | None,
) -> Iterator[None]:
    """Hold a cross-process lock around scan, build, and swap."""

    lock_path = dashboard_root / _LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise _BuildLockTimeout(
                        "another React dashboard build still owns the build lock"
                    )
                if heartbeat is not None:
                    heartbeat()
                time.sleep(0.25)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _swap_staged_dist(staging: Path, dist: Path, dashboard_root: Path) -> None:
    """Replace dist while preserving/restoring the last-good tree on failure."""

    backup = dashboard_root / f"{_BACKUP_PREFIX}{os.getpid()}-{time.time_ns()}"
    had_dist = dist.exists()
    if had_dist:
        dist.replace(backup)
    try:
        staging.replace(dist)
    except BaseException:
        if had_dist and backup.exists() and not dist.exists():
            backup.replace(dist)
        raise
    if backup.exists():
        _safe_remove_generated_tree(backup, dashboard_root)


def _terminate_build_process(proc: subprocess.Popen[str]) -> None:
    """Terminate npm and all descendants, then verify the owned handle exits."""

    if proc.poll() is not None:
        return
    if os.name == "nt":
        from work_buddy.compat import _force_kill_pid

        _force_kill_pid(proc.pid)
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=5.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_build_with_heartbeats(
    command: list[str],
    dashboard_root: Path,
    timeout_seconds: float,
    heartbeat: Callable[[], None] | None,
    heartbeat_interval_seconds: float,
    cancelled: Callable[[], bool] | None,
    process_started: Callable[[int], None] | None,
) -> subprocess.CompletedProcess[str]:
    """Run npm responsively and reap its whole process tree on every exit."""

    popen_kwargs: dict[str, Any] = {
        "cwd": dashboard_root,
        "env": os.environ.copy(),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
        "creationflags": subprocess_creation_flags(),
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(command, **popen_kwargs)
    try:
        if process_started is not None:
            process_started(proc.pid)
        deadline = time.monotonic() + timeout_seconds
        poll_interval = min(1.0, max(0.05, heartbeat_interval_seconds))
        next_heartbeat = time.monotonic() + max(
            0.05, heartbeat_interval_seconds,
        )
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:
                pass
        while True:
            if cancelled is not None and cancelled():
                raise _BuildCancelled("sidecar shutdown requested")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            try:
                stdout, stderr = proc.communicate(
                    timeout=min(poll_interval, remaining),
                )
                return subprocess.CompletedProcess(
                    command, proc.returncode, stdout, stderr,
                )
            except subprocess.TimeoutExpired:
                if heartbeat is not None and time.monotonic() >= next_heartbeat:
                    try:
                        heartbeat()
                    except Exception:
                        # State publication must not corrupt or cancel a build.
                        pass
                    next_heartbeat = time.monotonic() + max(
                        0.05, heartbeat_interval_seconds,
                    )
    finally:
        if proc.poll() is None:
            _terminate_build_process(proc)


def ensure_dashboard_react_build(
    *,
    timeout_seconds: float = _DEFAULT_BUILD_TIMEOUT_SECONDS,
    heartbeat: Callable[[], None] | None = None,
    heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    process_started: Callable[[int], None] | None = None,
) -> DashboardBuildResult:
    """Ensure the development React payload matches current source inputs."""

    started_at = time.monotonic()
    dashboard_root = _dashboard_root()
    dist_root = dashboard_root / "dist"
    marker_path = dist_root / _BUILD_MARKER_NAME

    checkout_problem = _development_checkout_problem(dashboard_root)
    if checkout_problem is None:
        if _validate_dist_payload(dist_root) is not None:
            _clear_error_marker(dashboard_root)
            return _result(
                DashboardBuildStatus.PACKAGED_DIST,
                "Using the packaged React dashboard payload.",
                dashboard_root,
                started_at,
            )
        return _failure(
            DashboardBuildStatus.MISSING_PACKAGED_DIST,
            "The packaged React dashboard payload is missing or invalid.",
            dashboard_root,
            started_at,
        )
    if checkout_problem:
        return _failure(
            DashboardBuildStatus.INCOMPLETE_DEVELOPMENT_CHECKOUT,
            f"The React development checkout is incomplete ({checkout_problem}).",
            dashboard_root,
            started_at,
        )

    try:
        with _dashboard_build_lock(
            dashboard_root,
            timeout_seconds=lock_timeout_seconds,
            heartbeat=heartbeat,
        ):
            _recover_generated_trees(dashboard_root, dist_root)
            try:
                snapshot = _snapshot_inputs(dashboard_root)
            except OSError as exc:
                return _failure(
                    DashboardBuildStatus.INPUT_SCAN_FAILED,
                    "The React dashboard inputs could not be inspected. See "
                    "the sidecar log for details.",
                    dashboard_root,
                    started_at,
                    diagnostic=str(exc),
                )

            payload = _validate_dist_payload(dist_root)
            marker = _load_marker(marker_path)
            if payload is not None and _marker_matches(
                marker, snapshot, payload,
            ):
                _clear_error_marker(dashboard_root)
                return _result(
                    DashboardBuildStatus.CURRENT_MARKER,
                    "React dashboard build matches the current inputs.",
                    dashboard_root,
                    started_at,
                    fingerprint=snapshot.fingerprint,
                )

            npm = _npm_executable()
            if npm is None:
                return _failure(
                    DashboardBuildStatus.NPM_UNAVAILABLE,
                    "React sources changed, but npm is unavailable. Install "
                    "Node.js/npm and restart the sidecar.",
                    dashboard_root,
                    started_at,
                    fingerprint=snapshot.fingerprint,
                )

            staging = Path(tempfile.mkdtemp(
                prefix=_STAGING_PREFIX, dir=dashboard_root,
            ))
            command = [
                npm, "run", "build", "--",
                "--outDir", str(staging),
                "--emptyOutDir",
            ]
            try:
                try:
                    completed = _run_build_with_heartbeats(
                        command,
                        dashboard_root,
                        timeout_seconds,
                        heartbeat,
                        heartbeat_interval_seconds,
                        cancelled,
                        process_started,
                    )
                except _BuildCancelled:
                    return _failure(
                        DashboardBuildStatus.BUILD_CANCELLED,
                        "The React dashboard build was cancelled during "
                        "sidecar shutdown.",
                        dashboard_root,
                        started_at,
                        fingerprint=snapshot.fingerprint,
                    )
                except subprocess.TimeoutExpired:
                    return _failure(
                        DashboardBuildStatus.BUILD_TIMED_OUT,
                        f"The React dashboard build exceeded {timeout_seconds:g} "
                        "seconds and was stopped.",
                        dashboard_root,
                        started_at,
                        fingerprint=snapshot.fingerprint,
                    )
                except OSError as exc:
                    return _failure(
                        DashboardBuildStatus.BUILD_FAILED,
                        "The React dashboard build process could not start. "
                        "See the sidecar log for details.",
                        dashboard_root,
                        started_at,
                        fingerprint=snapshot.fingerprint,
                        diagnostic=str(exc),
                    )

                if completed.returncode != 0:
                    return _failure(
                        DashboardBuildStatus.BUILD_FAILED,
                        "The automatic React dashboard build failed. If "
                        "frontend dependencies changed, run npm install in "
                        "dashboard-react, then restart the sidecar. See the "
                        "sidecar log for details.",
                        dashboard_root,
                        started_at,
                        fingerprint=snapshot.fingerprint,
                        returncode=completed.returncode,
                        diagnostic=_output_excerpt(completed),
                    )

                try:
                    after_build = _snapshot_inputs(dashboard_root)
                except OSError as exc:
                    return _failure(
                        DashboardBuildStatus.INPUT_SCAN_FAILED,
                        "The React inputs could not be verified after the "
                        "build. The last-good dashboard was preserved.",
                        dashboard_root,
                        started_at,
                        fingerprint=snapshot.fingerprint,
                        returncode=completed.returncode,
                        diagnostic=str(exc),
                    )
                if after_build.fingerprint != snapshot.fingerprint:
                    return _failure(
                        DashboardBuildStatus.INPUTS_CHANGED,
                        "React sources changed during the build. Restart the "
                        "sidecar to build the final source state.",
                        dashboard_root,
                        started_at,
                        fingerprint=after_build.fingerprint,
                        returncode=completed.returncode,
                    )

                staged_payload = _validate_dist_payload(staging)
                if staged_payload is None:
                    return _failure(
                        DashboardBuildStatus.INVALID_BUILD_OUTPUT,
                        "The React build completed without a valid dashboard "
                        "payload. The last-good dashboard was preserved.",
                        dashboard_root,
                        started_at,
                        fingerprint=after_build.fingerprint,
                        returncode=completed.returncode,
                    )

                try:
                    _write_build_marker(
                        staging / _BUILD_MARKER_NAME,
                        after_build,
                        staged_payload,
                    )
                except OSError as exc:
                    return _failure(
                        DashboardBuildStatus.MARKER_WRITE_FAILED,
                        "The React build was valid but its freshness record "
                        "could not be written. The last-good dashboard was "
                        "preserved.",
                        dashboard_root,
                        started_at,
                        fingerprint=after_build.fingerprint,
                        returncode=completed.returncode,
                        diagnostic=str(exc),
                    )
                try:
                    _swap_staged_dist(staging, dist_root, dashboard_root)
                except OSError as exc:
                    return _failure(
                        DashboardBuildStatus.BUILD_SWAP_FAILED,
                        "The validated React build could not replace the "
                        "dashboard payload. The last-good dashboard was "
                        "restored when possible.",
                        dashboard_root,
                        started_at,
                        fingerprint=after_build.fingerprint,
                        returncode=completed.returncode,
                        diagnostic=str(exc),
                    )

                _clear_error_marker(dashboard_root)
                return _result(
                    DashboardBuildStatus.BUILT,
                    "Built and installed the React dashboard from current inputs.",
                    dashboard_root,
                    started_at,
                    fingerprint=after_build.fingerprint,
                    returncode=completed.returncode,
                )
            finally:
                if staging.exists():
                    _safe_remove_generated_tree(staging, dashboard_root)
    except _BuildLockTimeout as exc:
        return _failure(
            DashboardBuildStatus.BUILD_LOCK_TIMED_OUT,
            "Timed out waiting for another React dashboard build to finish.",
            dashboard_root,
            started_at,
            diagnostic=str(exc),
        )
    except OSError as exc:
        return _failure(
            DashboardBuildStatus.INTERNAL_ERROR,
            "The React dashboard build guard hit a filesystem error. See "
            "the sidecar log for details.",
            dashboard_root,
            started_at,
            diagnostic=str(exc),
        )
