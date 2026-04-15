"""Session launcher — visible, persistent Claude Code sessions.

This is the fourth agent spawn pattern, distinct from the three sidecar
spawn modes in ``sidecar/dispatch/executor.py``:

- ``headless_ephemeral``: fire-and-forget ``claude -p`` (hidden)
- ``headless_persistent``: ``claude -p`` with session persistence (hidden)
- ``interactive_persistent``: PTY-spawned, visible in picker (hidden terminal)
- **session_launcher** (this module): real visible terminal, stays alive
  indefinitely. Supports two modes:
  - **Desktop mode**: standalone terminal, no ``--remote-control``.
  - **Mobile mode**: ``--remote-control`` enabled for phone app connection.

Use cases:
    - Dashboard "Launch Agent" buttons (desktop mode — no remote).
    - Telegram ``/remote`` command (mobile mode — with remote control).
    - Any context that needs a visible, interactive Claude Code session.

Design principles:
    - Fire-and-forget: launch, verify PID, return immediately.
    - Do NOT use the PTY adapter — that creates hidden terminals.
    - Do NOT capture output or wait for Claude to load.
    - Do NOT kill the process — it stays alive for the user.
    - Consent-gated via ``sidecar:remote_session_launch``.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from work_buddy.consent import ConsentCache
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).parent.parent

# Consent operation for remote session launches
REMOTE_SESSION_CONSENT_OP = "sidecar:remote_session_launch"


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

PlatformName = Literal["windows", "macos", "linux"]


def detect_platform() -> PlatformName:
    """Detect the current OS platform."""
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    elif system == "darwin":
        return "macos"
    else:
        return "linux"


# ---------------------------------------------------------------------------
# Visible terminal launcher (platform-specific)
# ---------------------------------------------------------------------------


def launch_visible_terminal(argv: list[str], cwd: str) -> int:
    """Launch a command in a new visible terminal window.

    This opens a real, user-visible terminal — not a hidden process.
    The terminal stays open and the process runs indefinitely.

    Args:
        argv: Command + arguments to run in the terminal.
        cwd: Working directory for the command.

    Returns:
        PID of the launched process (or wrapper process on macOS/Linux).

    Raises:
        RuntimeError: If no suitable terminal emulator is found.
        FileNotFoundError: If the command is not found.
    """
    plat = detect_platform()

    if plat == "windows":
        return _launch_windows(argv, cwd)
    elif plat == "macos":
        return _launch_macos(argv, cwd)
    else:
        return _launch_linux(argv, cwd)


def _launch_windows(argv: list[str], cwd: str) -> int:
    """Launch in a new PowerShell window via CREATE_NEW_CONSOLE.

    Wraps the command in ``powershell.exe -NoExit -Command`` so the
    terminal opens as PowerShell (not cmd.exe) with the proper
    environment. The ``-NoExit`` flag keeps the window open after
    the command finishes (or if it fails).
    """
    # Build a PowerShell command string from the argv list.
    # Each arg is single-quoted to handle spaces/special chars.
    ps_args = " ".join(_ps_quote(a) for a in argv)
    ps_command = f"cd {_ps_quote(cwd)}; & {ps_args}"

    # Strip ANTHROPIC_API_KEY so the child uses OAuth (Claude Max)
    # instead of API billing. Remote Control requires OAuth.
    env = _clean_env()

    # Use pwsh.exe (PowerShell 7) not powershell.exe (5.1 legacy).
    # The user's profile, conda, and claude auth all live in PS7.
    ps_exe = shutil.which("pwsh") or "powershell.exe"

    proc = subprocess.Popen(
        [ps_exe, "-NoExit", "-Command", ps_command],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        env=env,
        # Don't capture stdio — it's a visible interactive terminal
        stdin=None,
        stdout=None,
        stderr=None,
    )
    return proc.pid


def _clean_env() -> dict[str, str]:
    """Return a copy of os.environ with all Claude/Anthropic vars stripped.

    Remote Control requires the user's native ``claude.ai`` OAuth auth
    (Claude Max subscription). The parent process (MCP server, sidecar,
    or Claude Code itself) sets env vars like ``ANTHROPIC_API_KEY``,
    ``ANTHROPIC_AUTH_TOKEN``, ``CLAUDE_*``, etc. that override the
    user's normal auth and disable Remote Control.

    We strip ALL of these so the child process authenticates fresh
    using the user's own keychain/OAuth credentials.
    """
    env = os.environ.copy()
    to_remove = [
        k for k in env
        if k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_")
    ]
    for k in to_remove:
        del env[k]
    return env


def _ps_quote(s: str) -> str:
    """Escape a string for embedding in a PowerShell command.

    Uses single quotes with internal single quotes doubled.
    """
    return "'" + s.replace("'", "''") + "'"


def _launch_macos(argv: list[str], cwd: str) -> int:
    """Launch in a new Terminal.app window via osascript."""
    # Build a shell command that cd's and runs the command
    escaped_argv = " ".join(_shell_quote(a) for a in argv)
    escaped_cwd = _shell_quote(cwd)
    script = (
        f'tell application "Terminal" to do script '
        f'"cd {escaped_cwd} && {escaped_argv}"'
    )
    proc = subprocess.Popen(
        ["osascript", "-e", script],
        env=_clean_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # osascript returns quickly; the Terminal process is separate.
    # We return osascript's PID as a proxy — the actual terminal PID
    # is not easily retrievable.
    return proc.pid


def _launch_linux(argv: list[str], cwd: str) -> int:
    """Launch in a terminal emulator (tries common ones in order)."""
    escaped_argv = " ".join(_shell_quote(a) for a in argv)

    # Try terminal emulators in preference order
    terminals = [
        # gnome-terminal: --working-directory + --
        ("gnome-terminal", ["gnome-terminal", f"--working-directory={cwd}", "--", *argv]),
        # xterm: -e
        ("xterm", ["xterm", "-e", f"cd {_shell_quote(cwd)} && {escaped_argv}"]),
        # konsole: -e
        ("konsole", ["konsole", f"--workdir={cwd}", "-e", *argv]),
    ]

    for name, cmd in terminals:
        if shutil.which(name):
            proc = subprocess.Popen(
                cmd,
                env=_clean_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return proc.pid

    raise RuntimeError(
        "No terminal emulator found. Tried: gnome-terminal, xterm, konsole. "
        "Install one of these or set a custom terminal in config."
    )


def _shell_quote(s: str) -> str:
    """Shell-escape a string for embedding in a shell command."""
    import shlex
    return shlex.quote(s)


# ---------------------------------------------------------------------------
# Consent check
# ---------------------------------------------------------------------------


def _check_remote_session_consent() -> bool:
    """Check if remote session launching is consented."""
    try:
        cache = ConsentCache()
        return cache.is_granted(REMOTE_SESSION_CONSENT_OP)
    except Exception as exc:
        logger.warning("Consent check failed: %s — defaulting to deny.", exc)
        return False


# ---------------------------------------------------------------------------
# Session lookup (reuses pattern from executor.py)
# ---------------------------------------------------------------------------


def list_resumable_sessions(cwd: str | None = None) -> list[dict[str, Any]]:
    """Scan ~/.claude/sessions/ for resumable sessions.

    Args:
        cwd: If provided, filter to sessions started in this directory.

    Returns:
        List of session dicts sorted by startedAt (newest first).
        Each dict contains: pid, sessionId, name, cwd, startedAt, kind.
    """
    sessions_dir = Path.home() / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return []

    results = []
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if cwd and data.get("cwd", "").rstrip("/\\") != cwd.rstrip("/\\"):
                continue
            results.append({
                "pid": data.get("pid"),
                "session_id": data.get("sessionId"),
                "name": data.get("name", ""),
                "cwd": data.get("cwd", ""),
                "started_at": data.get("startedAt"),
                "kind": data.get("kind", ""),
            })
        except (OSError, json.JSONDecodeError):
            continue

    # Sort newest first
    results.sort(key=lambda x: x.get("started_at") or 0, reverse=True)
    return results


def _find_session_id(
    session_id: str | None = None,
    session_name: str | None = None,
    cwd: str | None = None,
) -> str | None:
    """Resolve a session to resume.

    Priority: explicit session_id > name match > most recent for cwd.

    ``session_id`` can be a full UUID or a partial prefix — it is resolved
    through the canonical :func:`resolve_session_id` resolver, which handles
    ambiguity detection.
    """
    if session_id:
        try:
            from work_buddy.sessions.inspector import resolve_session_id
            return resolve_session_id(session_id)
        except FileNotFoundError:
            # Resolver couldn't find it in JSONL sessions; fall back to
            # returning as-is so `claude --resume` can attempt it directly
            # (it may exist in ~/.claude/sessions/ but not in projects/).
            logger.debug(
                "resolve_session_id found no JSONL match for '%s', "
                "passing through to claude --resume", session_id,
            )
            return session_id

    sessions_dir = Path.home() / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return None

    for path in sorted(sessions_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))

            # Match by name
            if session_name and data.get("name") == session_name:
                return data.get("sessionId")

            # Match by cwd (most recent if no name given)
            if not session_name and cwd:
                if data.get("cwd", "").rstrip("/\\") == cwd.rstrip("/\\"):
                    return data.get("sessionId")

        except (OSError, json.JSONDecodeError):
            continue

    return None


# ---------------------------------------------------------------------------
# Public API: begin / list
# ---------------------------------------------------------------------------


def begin_session(
    cwd: str | None = None,
    prompt: str | None = None,
    session_id: str | None = None,
    session_name: str | None = None,
    bypass_permissions: bool = True,
    remote_control: bool = True,
) -> dict[str, Any]:
    """Launch or resume a visible Claude Code session in a real terminal.

    If ``session_id`` or ``session_name`` is provided, resumes that session.
    Otherwise, starts a new session.

    Consent-gated: requires grant for ``sidecar:remote_session_launch``.

    Args:
        cwd: Working directory. Defaults to repo root.
        prompt: Initial prompt for a new session. Ignored when resuming.
        session_id: Session ID to resume. Triggers resume mode.
        session_name: Session name to look up for resume. Triggers resume
            mode. Falls back to most recent session in cwd if the name
            isn't found.
        bypass_permissions: If True (default), adds
            ``--dangerously-skip-permissions`` so the session can operate
            without interactive permission prompts.
        remote_control: If True (default), adds ``--remote-control`` so
            the session can be connected to from Claude Desktop / Remote
            Control. Set to False for standalone terminal sessions.

    Returns:
        Dict with status, pid, and session details.
    """
    # --- Consent gate ---
    if not _check_remote_session_consent():
        return {
            "status": "consent_required",
            "operation": REMOTE_SESSION_CONSENT_OP,
            "reason": (
                "Opening a visible Claude Code session launches a real terminal "
                "window and starts an interactive AI session on your machine."
            ),
            "risk": "moderate",
            "default_ttl": 30,
        }

    if cwd is None:
        cwd = str(_REPO_ROOT)

    # --- Resume path ---
    if session_id or session_name:
        return _do_resume(
            session_id=session_id,
            session_name=session_name,
            cwd=cwd,
            bypass_permissions=bypass_permissions,
            remote_control=remote_control,
        )

    # --- New session path ---
    return _do_start(cwd=cwd, prompt=prompt, bypass_permissions=bypass_permissions,
                     remote_control=remote_control)


def _do_start(
    cwd: str,
    prompt: str | None,
    bypass_permissions: bool = True,
    remote_control: bool = True,
) -> dict[str, Any]:
    """Launch a new visible session."""
    # IMPORTANT: bare `claude` opens the REPL but doesn't start a
    # conversation — Remote Control can't connect to it. We always
    # need a prompt (positional arg) to kick off an actual session.
    if not prompt:
        prompt = "This session was launched via work-buddy. Stand by for instructions."

    # Prompt MUST be the positional arg (first after `claude`).
    # --remote-control takes an optional session name as its next arg,
    # so it must come AFTER the prompt to avoid eating it.
    cmd = ["claude", prompt]

    if remote_control:
        cmd.append("--remote-control")

    if bypass_permissions:
        cmd.append("--dangerously-skip-permissions")

    rc_label = "remote" if remote_control else "local"
    logger.info("Launching %s session: cwd='%s'", rc_label, cwd)

    pid = _launch_and_verify(cmd, cwd)
    if isinstance(pid, dict):
        return pid  # Error dict

    logger.info("Session launched (pid=%d, remote_control=%s)", pid, remote_control)

    if remote_control:
        message = (
            f"New session launched in a visible terminal. "
            f"PID: {pid}. Remote Control enabled.\n"
            f"Connect: https://claude.ai/code"
        )
    else:
        # TODO: Try claude-cli:// deep link approach — would open terminal
        # directly via URL scheme without server-side subprocess launch.
        message = (
            f"New session launched in a visible terminal. "
            f"PID: {pid}."
        )

    return {
        "status": "ok",
        "mode": "new",
        "pid": pid,
        "cwd": cwd,
        "remote_control": remote_control,
        "message": message,
    }


def _do_resume(
    session_id: str | None,
    session_name: str | None,
    cwd: str,
    bypass_permissions: bool = True,
    remote_control: bool = True,
) -> dict[str, Any]:
    """Resume an existing session in a visible terminal."""
    resolved_id = _find_session_id(
        session_id=session_id,
        session_name=session_name,
        cwd=cwd,
    )

    if not resolved_id:
        hint = f"name='{session_name}'" if session_name else f"id='{session_id}'"
        return {
            "status": "error",
            "error": f"No matching session found ({hint}). Use remote_session_list to see available sessions.",
        }

    cmd = ["claude", "--resume", resolved_id]

    if remote_control:
        cmd.append("--remote-control")

    if bypass_permissions:
        cmd.append("--dangerously-skip-permissions")

    rc_label = "remote" if remote_control else "local"
    logger.info(
        "Resuming %s session: session_id='%s', cwd='%s'",
        rc_label, resolved_id, cwd,
    )

    pid = _launch_and_verify(cmd, cwd)
    if isinstance(pid, dict):
        return pid  # Error dict

    if remote_control:
        message = (
            f"Resumed session '{resolved_id}' in a visible terminal. "
            f"PID: {pid}.\n"
            f"Connect: https://claude.ai/code"
        )
    else:
        message = (
            f"Resumed session '{resolved_id}' in a visible terminal. "
            f"PID: {pid}."
        )

    return {
        "status": "ok",
        "mode": "resume",
        "pid": pid,
        "session_id": resolved_id,
        "session_name": session_name or "",
        "cwd": cwd,
        "remote_control": remote_control,
        "message": message,
    }


def _launch_and_verify(cmd: list[str], cwd: str) -> int | dict[str, Any]:
    """Launch a terminal and verify the PID is alive.

    Returns the PID on success, or an error dict on failure.
    """
    try:
        pid = launch_visible_terminal(cmd, cwd)
    except (RuntimeError, FileNotFoundError) as exc:
        logger.error("Failed to launch terminal: %s", exc)
        return {"status": "error", "error": str(exc)}

    try:
        os.kill(pid, 0)  # Signal 0 = check existence
    except (OSError, ProcessLookupError):
        logger.warning("PID %d not alive immediately after spawn.", pid)
        return {
            "status": "error",
            "error": f"Process {pid} died immediately after launch.",
            "pid": pid,
        }

    return pid
