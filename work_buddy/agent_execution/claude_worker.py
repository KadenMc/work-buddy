"""Isolated Claude Code worker for one Co-work document-agent brief."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from work_buddy.compat import subprocess_creation_flags
from work_buddy.logging_config import get_logger
from work_buddy.sidecar.dispatch.executor import _build_headless_agent_argv

from .claude_code import (
    _isolated_setting_args,
    _work_buddy_mcp_config,
    claude_account_environment,
)
from .models import is_safe_session_id

logger = get_logger(__name__)

_MAX_PROMPT_CHARS = 1_000_000
_SUPPORTED_MODELS = frozenset({"sonnet", "opus"})
_SESSION_NAME = "daemon:work-buddy-cowork"


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

    child_env = claude_account_environment(os.environ)
    child_env["WORK_BUDDY_SESSION_ID"] = session_id
    argv = _build_headless_agent_argv(
        prompt=None,
        session_name=_SESSION_NAME,
        model=model,
        max_budget_usd=max_budget_usd,
        persistent=False,
        extra_args=(
            *_isolated_setting_args(),
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
