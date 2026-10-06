"""Host roles: the runtime context of work-buddy's own long-running processes.

A host is one of work-buddy's own Python processes. Each has a role, and the
role fixes its runtime context no matter how the host was started:

| Role | Interpreter | Windows image | Windows console | Identity |
|---|---|---|---|---|
| ``SIDECAR`` | the launcher's own (``sys.executable``) | ``python.exe`` | one of its own, with no window | a fresh ``sidecar-`` id, never inherited |
| ``SERVICE`` | the pinned child interpreter | ``python.exe`` | hidden, from ``CREATE_NO_WINDOW`` | the sidecar's, inherited |
| ``TRAY`` | the pinned child interpreter | ``pythonw.exe`` | none, by design | the CLI's, never inherited |

Why the console matters on Windows: a process with no console that starts a
console program without ``CREATE_NO_WINDOW`` makes Windows open a new,
visible window for that program. A background host that has a console with
no window passes it on to its console children, so they stay invisible even
when a library launches them without the flag. GUI hosts have no console,
and launch every child through this package's policy instead.

:func:`start_host` starts a host with its role's context.
:func:`establish_host_context` repairs the context from inside a host whose
launcher could not provide it, for example a logon task that starts
``pythonw.exe``. :func:`describe_host_context` reports the context a host
actually has.

Standard library only at module level. ``work_buddy.config`` and
``work_buddy.paths`` are imported inside the functions that need them.
"""

from __future__ import annotations

import enum
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from work_buddy.process import policy
from work_buddy.process.runner import ProcessHandle, ProcessRunner, current_runner

logger = logging.getLogger(__name__)

RELAUNCH_MARKER = "WORK_BUDDY_HOST_RELAUNCHED"
"""Set in a host relaunched to gain a console, so it never relaunches again."""

FORCE_RELAUNCH = "WORK_BUDDY_HOST_FORCE_RELAUNCH"
"""Tests only: behave as if the in-process console API were unavailable."""

IDENTITY_VARIABLES = ("WORK_BUDDY_SESSION_ID", "CODEX_THREAD_ID")
"""Environment variables that carry an agent session's identity."""

CLI_SESSION_ID = "wbuddy-cli"
"""The synthetic identity of the ``wbuddy`` CLI, which the tray shares."""

_ALLOC_CONSOLE_MODE_NO_WINDOW = 2


class HostRole(str, enum.Enum):
    SIDECAR = "sidecar"
    SERVICE = "service"
    TRAY = "tray"


@dataclass(frozen=True)
class RoleSpec:
    """What a role requires of its runtime context."""

    image: str
    """``"console"`` (``python.exe``) or ``"gui"`` (``pythonw.exe``) on Windows."""
    pinned_interpreter: bool
    """Start on the pinned child interpreter, rather than the launcher's own."""
    inherits_identity: bool
    detached: bool
    """POSIX: start in a new session by default."""
    unbuffered: bool
    """Pass ``-u``, so output reaches the host's log file immediately."""
    child_env: bool
    """Apply :func:`build_child_env` to the inherited environment."""


ROLE_SPECS: Mapping[HostRole, RoleSpec] = {
    # The sidecar's environment is exactly the one its launcher has, as when
    # the logon task starts it, so every start path yields the same daemon.
    # The sidecar applies build_child_env to its own children.
    HostRole.SIDECAR: RoleSpec(
        image="console", pinned_interpreter=False, inherits_identity=False,
        detached=True, unbuffered=False, child_env=False,
    ),
    HostRole.SERVICE: RoleSpec(
        image="console", pinned_interpreter=True, inherits_identity=True,
        detached=False, unbuffered=True, child_env=True,
    ),
    HostRole.TRAY: RoleSpec(
        image="gui", pinned_interpreter=True, inherits_identity=False,
        detached=True, unbuffered=False, child_env=True,
    ),
}


# ---------------------------------------------------------------------------
# Interpreter and environment
# ---------------------------------------------------------------------------


def resolve_child_python(cfg: dict | None = None) -> str:
    """The Python interpreter work-buddy's child hosts run on.

    1. ``sidecar.python_executable`` from config, when set and pointing at an
       existing file. This pins the interpreter independently of how the
       parent was launched, so a parent started on the wrong interpreter
       still starts its children on the right one.
    2. Otherwise ``sys.executable``, this process's own interpreter.

    A pin naming a missing file falls back to ``sys.executable`` and logs an
    error. A pin that differs from ``sys.executable`` logs a warning: useful
    to see, but the pin is the user's explicit intent, so it wins.
    """
    if cfg is None:
        from work_buddy.config import load_config

        try:
            cfg = load_config()
        except Exception:
            cfg = {}
    pinned = (cfg.get("sidecar", {}) or {}).get("python_executable")
    if not pinned:
        return sys.executable
    if not Path(pinned).is_file():
        logger.error(
            "sidecar.python_executable=%r does not exist; falling back to "
            "sys.executable=%r. Children may spawn on the wrong interpreter.",
            pinned, sys.executable,
        )
        return sys.executable
    if Path(pinned).resolve() != Path(sys.executable).resolve():
        logger.warning(
            "sidecar.python_executable=%r differs from sys.executable=%r; the "
            "parent and its children will run on different interpreters. This is "
            "intentional only if you explicitly pinned an env different from the "
            "parent's own.",
            pinned, sys.executable,
        )
    return pinned


def build_child_env() -> dict[str, str]:
    """The environment for a Python child that work-buddy starts.

    Adds two defaults, each with ``setdefault`` so an explicit user value
    wins:

    * ``PYTHONUTF8=1``, so the child's stdout and stderr are UTF-8. Without
      it, a Windows child writes in the ANSI code page, and logging raises
      ``UnicodeEncodeError`` on text outside Latin-1.
    * ``OPENBLAS_NUM_THREADS=1``, before the child can import NumPy. Each of
      work-buddy's Python services would otherwise reserve a machine-wide
      BLAS thread pool, far more memory than request-sized scoring needs.

    Returns a fresh dict and never changes ``os.environ``.
    """
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    return env


def sibling_image(
    python: str, image: str, *, platform: str | None = None,
) -> tuple[str, bool]:
    """The interpreter beside ``python`` with the role's Windows image.

    Returns ``(path, matched)``. On Windows, ``image="console"`` selects
    ``python.exe`` and ``image="gui"`` selects ``pythonw.exe`` from the same
    directory, so the choice of image never changes which environment runs.
    When the sibling is missing, ``python`` comes back unchanged with
    ``matched=False``, for the caller to report. On POSIX there is only one
    image.
    """
    if not policy.is_windows(platform):
        return python, True
    wanted = "pythonw.exe" if image == "gui" else "python.exe"
    path = Path(python)
    if path.name.lower() == wanted:
        return python, True
    candidate = path.with_name(wanted)
    if candidate.is_file():
        return str(candidate), True
    return python, False


def image_of(python: str, *, platform: str | None = None) -> str:
    """``"gui"`` for a ``pythonw.exe`` on Windows, otherwise ``"console"``."""
    if policy.is_windows(platform) and Path(python).name.lower() == "pythonw.exe":
        return "gui"
    return "console"


def host_env(
    role: HostRole,
    extra_env: Mapping[str, str] | None = None,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment a host of ``role`` starts with.

    Roles that never inherit an identity get none of their launcher's. The
    sidecar assigns its own at its entry point, and the tray starts with its
    own.
    """
    spec = ROLE_SPECS[role]
    if base_env is not None:
        env = dict(base_env)
    elif spec.child_env:
        env = build_child_env()
    else:
        env = dict(os.environ)
    if not spec.inherits_identity:
        for name in IDENTITY_VARIABLES:
            env.pop(name, None)
    if role is HostRole.TRAY:
        # The tray's own identity, set before it starts: importing the tray
        # package may already need one.
        env["WORK_BUDDY_SESSION_ID"] = CLI_SESSION_ID
    if extra_env:
        env.update(extra_env)
    return env


# ---------------------------------------------------------------------------
# Starting a host
# ---------------------------------------------------------------------------


def start_host(
    role: HostRole,
    module: str,
    *,
    args: Sequence[str] = (),
    log_path: str | os.PathLike[str] | None = None,
    name: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    python: str | None = None,
    detached: bool | None = None,
    runner: ProcessRunner | None = None,
) -> ProcessHandle:
    """Start ``python -m module`` as a work-buddy host with ``role``'s context.

    ``log_path`` sends the host's stdout and stderr to that file, appended
    after a start banner naming ``name`` (default: the module). Without it,
    the host's stdio is discarded: hosts log to their own files.

    ``python`` overrides the role's interpreter. The role's Windows image is
    still applied to it. ``cwd`` defaults to the repository root.
    ``detached`` overrides the role's POSIX session default.

    Raises :class:`~work_buddy.process.ExecutableNotFound` when the
    interpreter is missing, and ``OSError`` when the launch fails.
    """
    spec = ROLE_SPECS[role]
    if python is None:
        python = resolve_child_python() if spec.pinned_interpreter else sys.executable
    executable, matched = sibling_image(python, spec.image)
    if not matched:
        logger.warning(
            "No %s interpreter beside %s, so %s starts on it unchanged.",
            "pythonw.exe" if spec.image == "gui" else "python.exe",
            python, module,
        )
    argv = [executable, *(["-u"] if spec.unbuffered else []), "-m", module, *args]
    if cwd is None:
        from work_buddy import paths

        cwd = paths.repo_root()

    log_handle = None
    if log_path is not None:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
            log_handle.write(
                f"\n--- {name or module} starting at "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
            )
            log_handle.flush()
        except OSError as exc:
            logger.error("Could not open %s for %s output: %s", log_path, name or module, exc)
            log_handle = None

    try:
        launch = policy.resolve_host_launch(
            argv,
            cwd=cwd,
            env=host_env(role, extra_env),
            stdout=log_handle if log_handle is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_handle is not None else subprocess.DEVNULL,
            detached=spec.detached if detached is None else detached,
        )
        return (runner or current_runner()).spawn(launch)
    finally:
        # The host holds its own copy of the log handle. Keeping this one
        # open would leak a handle per start and, on Windows, pin the file
        # against the next start's log roll.
        if log_handle is not None:
            log_handle.close()


# ---------------------------------------------------------------------------
# Establishing and describing the context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostContext:
    """The runtime context a host actually has.

    ``console`` is ``allocated`` (created by this host, with no window),
    ``hidden`` (inherited, with no window), ``attached`` (inherited, with a
    window, as in a terminal), ``none``, or ``not_applicable`` (POSIX, and GUI
    hosts). ``mechanism`` says how the console came about: ``inherited``,
    ``allocate_api``, ``relaunch``, ``foreground``, ``none``, or
    ``not_applicable``.
    """

    role: str
    executable: str
    image: str
    console: str
    mechanism: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


_established: HostContext | None = None


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes as wt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = wt.HWND
    kernel32.GetConsoleCP.restype = wt.UINT
    return kernel32


def _console_state() -> tuple[bool, bool]:
    """Windows: ``(has_console, console_has_window)`` for this process."""
    kernel32 = _kernel32()
    return kernel32.GetConsoleCP() != 0, bool(kernel32.GetConsoleWindow())


def _allocate_windowless_console() -> bool:
    """Windows 11 24H2 and later: give this process a console with no window.

    Uses ``AllocConsoleWithOptions`` with ``ALLOC_CONSOLE_MODE_NO_WINDOW``.
    Returns ``False`` where the API does not exist or the call fails.
    """
    import ctypes
    from ctypes import wintypes as wt

    class ALLOC_CONSOLE_OPTIONS(ctypes.Structure):
        _fields_ = [
            ("mode", ctypes.c_int),
            ("useShowWindow", wt.BOOL),
            ("showWindow", wt.WORD),
        ]

    kernel32 = _kernel32()
    try:
        allocate = kernel32.AllocConsoleWithOptions
    except AttributeError:
        return False
    allocate.argtypes = [ctypes.POINTER(ALLOC_CONSOLE_OPTIONS), ctypes.POINTER(ctypes.c_int)]
    allocate.restype = ctypes.c_long
    options = ALLOC_CONSOLE_OPTIONS(_ALLOC_CONSOLE_MODE_NO_WINDOW, False, 0)
    result = ctypes.c_int(-1)
    try:
        hresult = allocate(ctypes.byref(options), ctypes.byref(result))
    except OSError as exc:
        logger.error("AllocConsoleWithOptions failed: %s", exc)
        return False
    if hresult < 0:
        logger.error("AllocConsoleWithOptions failed: HRESULT 0x%08X", hresult & 0xFFFFFFFF)
        return False
    return _console_state()[0]


def signed_exit_status(status: int) -> int:
    """Map an unsigned Windows exit status onto the signed range that
    ``sys.exit`` accepts. The process still exits with the same 32 bits."""
    return status - 2**32 if status >= 2**31 else status


def _entry_module() -> str | None:
    """The module this process was started with, as in ``python -m <module>``."""
    main = sys.modules.get("__main__")
    name = getattr(getattr(main, "__spec__", None), "name", None)
    if not name:
        return None
    return name[: -len(".__main__")] if name.endswith(".__main__") else name


def _relaunch_with_console(module: str | None, runner: ProcessRunner | None) -> None:
    """Relaunch this host under the console interpreter, with a console of
    its own and no window, wait for it, and exit with its exit status.

    Returns only when the relaunch cannot be attempted or fails to start.

    The parent holds the relaunched host in a kill-on-close job, so ending
    the parent ends the host, just as a uv venv launcher ties its own
    interpreter's lifetime to it. The job allows silent breakaway, so the
    host's own children keep the job the host gives them.
    """
    from work_buddy.process import tree

    module = module or _entry_module()
    if module is None:
        logger.error("Cannot relaunch for a console: this process was not started with -m.")
        return
    python, matched = sibling_image(sys.executable, "console")
    if not matched:
        logger.error("Cannot relaunch for a console: no python.exe beside %s.", sys.executable)
        return
    env = dict(os.environ)
    env[RELAUNCH_MARKER] = "1"
    launch = policy.resolve_host_launch(
        [python, "-m", module, *sys.argv[1:]],
        cwd=os.getcwd(),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        detached=False,
    )
    try:
        child = (runner or current_runner()).spawn(launch)
    except OSError as exc:
        logger.error("Relaunch for a console failed to start: %s", exc)
        return
    job = tree.create_kill_on_close_job(silent_breakaway=True)
    if job is None or not tree.assign_process_to_job(job, child.pid):
        logger.warning(
            "The relaunched host (pid=%d) is not tied to this process: ending "
            "this process will not end it. Stopping work-buddy still will.",
            child.pid,
        )
    status = child.wait()
    raise SystemExit(signed_exit_status(status))


def claim_identity(role: HostRole) -> None:
    """Give this host its role's identity, never one inherited from a caller.

    A sidecar gets a fresh ``sidecar-`` id: it is the sidecar's own consent
    principal. The tray runs lifecycle operations under the CLI's synthetic
    identity. A service keeps the identity its sidecar gave it.
    """
    if role is HostRole.SIDECAR:
        import uuid

        os.environ["WORK_BUDDY_SESSION_ID"] = f"sidecar-{uuid.uuid4().hex[:8]}"
    elif role is HostRole.TRAY:
        os.environ["WORK_BUDDY_SESSION_ID"] = CLI_SESSION_ID
    if not ROLE_SPECS[role].inherits_identity:
        os.environ.pop("CODEX_THREAD_ID", None)


def establish_host_context(
    role: HostRole,
    *,
    foreground: bool = False,
    module: str | None = None,
    runner: ProcessRunner | None = None,
) -> HostContext:
    """Give this host the runtime context its role requires, then its identity.

    Call it as the first statement of a host's entry point, before anything
    that could start a process. On Windows, a background console role with
    no console gets one without a window:

    1. A console is already attached: nothing to do.
    2. Otherwise, on Windows 11 24H2 and later, allocate one in-process.
    3. Otherwise, relaunch under the console interpreter with
       ``CREATE_NO_WINDOW``, wait, and exit with the relaunched host's exit
       status. This function does not return in that process.
    4. If both fail, log an error and carry on without a console, so status
       can report it. A host never refuses to start over its console.

    ``foreground=True`` is a host running in the user's terminal: it keeps
    that terminal's console. ``module`` overrides the module to relaunch,
    which defaults to the one this process was started with.
    """
    global _established
    spec = ROLE_SPECS[role]
    windows = policy.is_windows()
    if foreground:
        console = _observe_console() if windows else "not_applicable"
        mechanism = "foreground"
    elif not windows or spec.image == "gui":
        console = mechanism = "not_applicable"
    else:
        has_console, has_window = _console_state()
        if os.environ.get(RELAUNCH_MARKER) == "1":
            mechanism = "relaunch"
            console = _describe(has_console, has_window, allocated=False)
            if not has_console:
                logger.error("Relaunched for a console, but still have none.")
        elif has_console:
            mechanism = "inherited"
            console = _describe(has_console, has_window, allocated=False)
        elif os.environ.get(FORCE_RELAUNCH) != "1" and _allocate_windowless_console():
            mechanism = "allocate_api"
            console = "allocated"
        else:
            _relaunch_with_console(module, runner)
            logger.error(
                "Running without a console: console programs this host "
                "starts without the no-window flag will open windows."
            )
            mechanism = console = "none"
    claim_identity(role)
    _established = HostContext(
        role=role.value,
        executable=sys.executable,
        image=image_of(sys.executable),
        console=console,
        mechanism=mechanism,
    )
    return _established


def _describe(has_console: bool, has_window: bool, *, allocated: bool) -> str:
    if not has_console:
        return "none"
    if has_window:
        return "attached"
    return "allocated" if allocated else "hidden"


def _observe_console() -> str:
    has_console, has_window = _console_state()
    return _describe(has_console, has_window, allocated=False)


def describe_host_context(cfg: dict | None = None) -> dict[str, Any]:
    """The runtime context of this host, for the state file and status.

    Adds the child interpreter to what :func:`establish_host_context`
    recorded: the interpreter and image work-buddy's services run on, the
    configured pin, and whether the pin points outside the project.
    """
    context = _established
    if context is None:
        windows = policy.is_windows()
        console = _observe_console() if windows else "not_applicable"
        context = HostContext(
            role="unknown",
            executable=sys.executable,
            image=image_of(sys.executable),
            console=console,
            mechanism="inherited" if console not in ("none", "not_applicable") else console,
        )
    if cfg is None:
        from work_buddy.config import load_config

        try:
            cfg = load_config()
        except Exception:
            cfg = {}
    pin = (cfg.get("sidecar", {}) or {}).get("python_executable") or None
    child_python, _ = sibling_image(resolve_child_python(cfg), "console")
    record: dict[str, Any] = context.to_dict()
    record.update(
        child_python=child_python,
        child_image=image_of(child_python),
        pin=pin,
        pin_outside_project=_outside_project(pin) if pin else False,
    )
    return record


def _outside_project(path: str) -> bool:
    from work_buddy import paths

    try:
        Path(path).resolve().relative_to(Path(paths.repo_root()).resolve())
    except ValueError:
        return True
    except OSError:
        return False
    return False
