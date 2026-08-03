"""Isolated Claude Code worker for one Co-work document-agent brief."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from work_buddy.compat import subprocess_creation_flags
from work_buddy.logging_config import get_logger
from work_buddy.sidecar.dispatch.executor import _build_headless_agent_argv

from .claude_code import (
    _isolated_worker_setting_args,
    _work_buddy_mcp_config,
    claude_account_environment,
)
from .models import is_safe_session_id

logger = get_logger(__name__)

_MAX_PROMPT_CHARS = 1_000_000
_SUPPORTED_MODELS = frozenset({"sonnet", "opus"})
_SESSION_NAME = "daemon:work-buddy-cowork"
_CLAUDE_CREDENTIALS_FILENAME = ".credentials.json"
_CLAUDE_USES_KEYCHAIN = sys.platform == "darwin"
_CLEANUP_RETRY_DELAYS = (0.05, 0.15, 0.3)


def _claude_config_source(environment: Mapping[str, str]) -> Path:
    """Resolve the Claude account store selected by the trusted launcher."""

    configured = environment.get("CLAUDE_CONFIG_DIR", "").strip()
    source = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return source.resolve()


def _write_account_credentials(source: Path, destination: Path) -> None:
    """Project only Claude account auth into a private credential file."""

    try:
        source_payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise OSError("Claude account credentials are unavailable") from exc
    account_credentials = (
        source_payload.get("claudeAiOauth")
        if isinstance(source_payload, dict)
        else None
    )
    if not isinstance(account_credentials, dict) or not account_credentials:
        raise OSError("Claude account credentials are unavailable")
    projected_payload = json.dumps(
        {"claudeAiOauth": account_credentials},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    destination_fd = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(
            destination_fd,
            "wb",
        ) as destination_file:
            destination_fd = -1
            destination_file.write(projected_payload)
        if os.name != "nt":
            destination.chmod(0o600)
    except BaseException:
        if destination_fd >= 0:
            os.close(destination_fd)
        destination.unlink(missing_ok=True)
        raise


def _retry_cleanup(
    operation: Callable[[], None],
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Retry transient cleanup failures for a bounded half second."""

    for delay in (*_CLEANUP_RETRY_DELAYS, None):
        try:
            operation()
            return
        except FileNotFoundError:
            return
        except OSError:
            if delay is None:
                raise
            sleeper(delay)


def _cleanup_isolated_config(
    temporary: TemporaryDirectory[str],
    isolated_credentials: Path,
) -> None:
    """Remove credential material first, then its bounded temporary root."""

    credential_error: OSError | None = None
    try:
        _retry_cleanup(
            lambda: isolated_credentials.unlink(missing_ok=True)
        )
    except OSError as exc:
        credential_error = exc
    try:
        _retry_cleanup(temporary.cleanup)
    except OSError as exc:
        if credential_error is not None:
            raise exc from credential_error
        raise


@contextmanager
def _isolated_claude_config(
    environment: Mapping[str, str],
) -> Iterator[Path]:
    """Expose only the signed-in account credential to one Claude process.

    The preferred directory is created beside the source credential, under the
    source config directory's access controls. On Windows and Linux, a private
    projection includes only ``claudeAiOauth`` and excludes unrelated MCP OAuth
    credentials. macOS uses Keychain and never seeds the credential file. Only
    that credential-free path may fall back to the system temporary directory.
    """

    source_config = _claude_config_source(environment)
    source_credentials = source_config / _CLAUDE_CREDENTIALS_FILENAME
    seed_account_credentials = not _CLAUDE_USES_KEYCHAIN
    if seed_account_credentials and not source_credentials.is_file():
        raise FileNotFoundError("Claude account credentials are unavailable")

    try:
        temporary = TemporaryDirectory(
            prefix=".work-buddy-claude-config-",
            dir=source_config,
        )
    except (PermissionError, OSError):
        if seed_account_credentials:
            raise
        temporary = TemporaryDirectory(
            prefix="work-buddy-claude-config-",
        )

    isolated_config = Path(temporary.name).resolve()
    isolated_credentials = isolated_config / _CLAUDE_CREDENTIALS_FILENAME
    try:
        if os.name != "nt":
            isolated_config.chmod(0o700)
        if seed_account_credentials:
            _write_account_credentials(
                source_credentials,
                isolated_credentials,
            )
        yield isolated_config
    finally:
        _cleanup_isolated_config(temporary, isolated_credentials)


def run_worker(
    *,
    model: str,
    prompt: str,
    session_id: str,
    max_budget_usd: float,
    command_runner: Callable[..., Any] = subprocess.run,
) -> int:
    """Run Claude with user customizations disabled in a neutral directory.

    Administrator-managed Claude Code policy remains effective as part of the
    host's administrative trust boundary.
    """

    if (
        model not in _SUPPORTED_MODELS
        or not prompt
        or not is_safe_session_id(session_id)
        or max_budget_usd <= 0
    ):
        return 2

    argv = _build_headless_agent_argv(
        prompt=None,
        session_name=_SESSION_NAME,
        model=model,
        max_budget_usd=max_budget_usd,
        persistent=False,
        extra_args=(
            *_isolated_worker_setting_args(),
            "--mcp-config",
            _work_buddy_mcp_config(session_id),
            "--strict-mcp-config",
            "--tools",
            "",
            "--disable-slash-commands",
            "--no-chrome",
        ),
    )

    try:
        with _isolated_claude_config(os.environ) as isolated_config:
            child_env = claude_account_environment(os.environ)
            child_env["CLAUDE_CONFIG_DIR"] = str(isolated_config)
            child_env["WORK_BUDDY_SESSION_ID"] = session_id
            with TemporaryDirectory(
                prefix="work-buddy-claude-host-",
                ignore_cleanup_errors=True,
            ) as host_directory:
                result = command_runner(
                    argv,
                    cwd=str(Path(host_directory).resolve()),
                    env=child_env,
                    input=prompt,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=False,
                    shell=False,
                    creationflags=subprocess_creation_flags(),
                )
        return 0 if int(getattr(result, "returncode", 1)) == 0 else 1
    except (FileNotFoundError, PermissionError, OSError):
        logger.error("Claude document worker could not open the runtime")
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", required=True, choices=sorted(_SUPPORTED_MODELS))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--max-budget-usd", required=True, type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    prompt = sys.stdin.read(_MAX_PROMPT_CHARS + 1)
    if len(prompt) > _MAX_PROMPT_CHARS:
        return 2
    return run_worker(
        model=args.model,
        prompt=prompt,
        session_id=args.session_id,
        max_budget_usd=args.max_budget_usd,
    )


if __name__ == "__main__":
    raise SystemExit(main())
