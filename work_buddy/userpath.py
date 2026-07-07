"""Expose the ``wbuddy`` CLI on the user's PATH, per-user and reversible.

The native installers never put the venv's ``Scripts``/``bin`` directory on
PATH (that would expose ``python.exe``/``pip`` globally). Instead, provisioning
publishes exactly one command:

- **Windows**: a ``bin\\wbuddy.cmd`` shim inside the install HOME forwards to
  ``.venv\\Scripts\\wbuddy.exe``, and that ``bin`` directory is appended to the
  per-user PATH (``HKCU\\Environment``, no elevation). A ``WM_SETTINGCHANGE``
  broadcast lets newly opened shells pick it up without a logoff.
- **POSIX**: a ``~/.local/bin/wbuddy`` shim execs ``.venv/bin/wbuddy``.
  ``~/.local/bin`` is on PATH by convention (systemd distros and macOS shells
  include it via the default profile); when it is not, the caller surfaces that
  in its step detail rather than editing shell rc files.

Uninstall (``provision.uninstall``) removes the shim and, on Windows, strips
the PATH segment again. The PATH string surgery lives in pure functions
(:func:`merge_path`, :func:`strip_path`) so it is unit-testable without
touching the registry.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from work_buddy.compat import IS_WINDOWS
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_WIN_SHIM = '@"%~dp0..\\.venv\\Scripts\\wbuddy.exe" %*\n'


# --- pure PATH-string surgery (unit-tested) --------------------------------

def _norm(segment: str) -> str:
    """Normalize a PATH segment for comparison (Windows: case + trailing sep)."""
    seg = segment.strip().rstrip("\\/")
    return seg.casefold() if IS_WINDOWS else seg


def merge_path(current: str, entry: str) -> str | None:
    """Return ``current`` with ``entry`` appended, or None if already present.

    Preserves the existing value byte-for-byte (including ``%VAR%`` references
    in a ``REG_EXPAND_SZ`` value, which must never be expanded and re-written).
    """
    segments = [s for s in current.split(os.pathsep) if s.strip()]
    if any(_norm(s) == _norm(entry) for s in segments):
        return None
    if not segments:
        return entry
    return current.rstrip(os.pathsep) + os.pathsep + entry


def strip_path(current: str, entry: str) -> str | None:
    """Return ``current`` without ``entry``, or None if it was absent."""
    segments = current.split(os.pathsep)
    kept = [s for s in segments if not (s.strip() and _norm(s) == _norm(entry))]
    if len(kept) == len(segments):
        return None
    return os.pathsep.join(s for s in kept if s.strip())


# --- Windows per-user PATH (HKCU\Environment) ------------------------------

def _read_user_path() -> tuple[str, int]:
    """Return the raw per-user PATH value and its registry type.

    ``winreg.QueryValueEx`` does NOT expand ``REG_EXPAND_SZ`` values, so
    ``%VAR%`` references come back literally and can be written back intact.
    A missing value reads as an empty ``REG_EXPAND_SZ``.
    """
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            value, regtype = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ
    return str(value), int(regtype)


def _write_user_path(value: str, regtype: int) -> None:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "Path", 0, regtype, value)


def _broadcast_environment_change() -> None:
    """Tell running shells the environment changed (best-effort)."""
    import ctypes

    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_ulong()),
        )
    except Exception:  # pragma: no cover - cosmetic; new logins still see it
        logger.debug("WM_SETTINGCHANGE broadcast failed", exc_info=True)


def add_dir_to_user_path(directory: str) -> dict:
    """Append ``directory`` to the per-user PATH (idempotent, Windows only)."""
    if not IS_WINDOWS:
        return {"ok": True, "changed": False, "detail": "POSIX: PATH not managed"}
    try:
        current, regtype = _read_user_path()
        merged = merge_path(current, directory)
        if merged is None:
            return {"ok": True, "changed": False, "detail": f"{directory} already on user PATH"}
        _write_user_path(merged, regtype)
        _broadcast_environment_change()
        return {"ok": True, "changed": True, "detail": f"added {directory} to user PATH"}
    except OSError as exc:
        return {"ok": False, "changed": False, "detail": f"user PATH update failed: {exc}"}


def remove_dir_from_user_path(directory: str) -> dict:
    """Remove ``directory`` from the per-user PATH (idempotent, Windows only)."""
    if not IS_WINDOWS:
        return {"ok": True, "changed": False, "detail": "POSIX: PATH not managed"}
    try:
        current, regtype = _read_user_path()
        stripped = strip_path(current, directory)
        if stripped is None:
            return {"ok": True, "changed": False, "detail": f"{directory} was not on user PATH"}
        _write_user_path(stripped, regtype)
        _broadcast_environment_change()
        return {"ok": True, "changed": True, "detail": f"removed {directory} from user PATH"}
    except OSError as exc:
        return {"ok": False, "changed": False, "detail": f"user PATH update failed: {exc}"}


# --- the shim itself --------------------------------------------------------

def _venv_wbuddy(home: Path) -> Path:
    if IS_WINDOWS:
        return home / ".venv" / "Scripts" / "wbuddy.exe"
    return home / ".venv" / "bin" / "wbuddy"


def _posix_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def install_cli_shim(home: str | Path) -> dict:
    """Publish ``wbuddy`` on the user's PATH for the install at ``home``.

    Creates only a one-command shim; the venv's own ``Scripts``/``bin`` never
    lands on PATH. No-ops (with detail) when the venv CLI does not exist, so
    source checkouts without a ``.venv`` are unaffected.
    """
    home = Path(home).resolve()
    target = _venv_wbuddy(home)
    if not target.exists():
        return {"ok": True, "changed": False, "detail": f"no venv CLI at {target}; shim skipped"}

    if IS_WINDOWS:
        bin_dir = home / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "wbuddy.cmd").write_text(_WIN_SHIM, encoding="utf-8")
        path_res = add_dir_to_user_path(str(bin_dir))
        detail = f"wrote {bin_dir / 'wbuddy.cmd'}; {path_res['detail']}"
        return {"ok": path_res["ok"], "changed": True, "detail": detail}

    bin_dir = _posix_bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "wbuddy"
    shim.write_text(f'#!/bin/sh\nexec "{target}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    on_path = any(_norm(str(bin_dir)) == _norm(s) for s in os.environ.get("PATH", "").split(os.pathsep))
    hint = "" if on_path else f" (note: {bin_dir} is not on PATH in this shell)"
    return {"ok": True, "changed": True, "detail": f"wrote {shim}{hint}"}


def uninstall_cli_shim(home: str | Path) -> dict:
    """Remove the shim and (Windows) the PATH entry for the install at ``home``.

    On POSIX the shared ``~/.local/bin/wbuddy`` is removed only when it points
    at this install, so uninstalling one copy never breaks another.
    """
    home = Path(home).resolve()
    details: list[str] = []

    if IS_WINDOWS:
        bin_dir = home / "bin"
        shim = bin_dir / "wbuddy.cmd"
        if shim.exists():
            shim.unlink()
            details.append(f"removed {shim}")
        try:
            bin_dir.rmdir()  # only if empty
        except OSError:
            pass
        details.append(remove_dir_from_user_path(str(bin_dir))["detail"])
        return {"ok": True, "detail": "; ".join(details)}

    shim = _posix_bin_dir() / "wbuddy"
    if shim.exists():
        try:
            points_here = str(home) in shim.read_text(encoding="utf-8")
        except OSError:
            points_here = False
        if points_here:
            shim.unlink()
            details.append(f"removed {shim}")
        else:
            details.append(f"left {shim} (points at a different install)")
    else:
        details.append("no shim present")
    return {"ok": True, "detail": "; ".join(details)}
