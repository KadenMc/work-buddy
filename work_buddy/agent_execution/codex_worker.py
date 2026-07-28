"""Isolated long-running Codex SDK worker for a document-agent brief."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

from work_buddy.logging_config import get_logger

from .codex import (
    codex_chatgpt_environment,
    codex_entry_isolation_overrides,
    codex_subscription_config,
    effective_mcp_server_names,
    effective_plugin_ids,
    read_effective_codex_config,
    validate_entry_isolation,
    validate_subscription_codex_config,
)
from .models import is_safe_session_id

logger = get_logger(__name__)

_MAX_PROMPT_CHARS = 1_000_000
_COWORK_BASE_INSTRUCTIONS = (
    "You are the Work Buddy Co-work document agent. Work only through the "
    "Work Buddy MCP server exposed to this thread. Do not use or request "
    "filesystem, shell, browser, computer-use, app, plugin, skill, web-search, "
    "image, or other MCP access."
)
_COWORK_DEVELOPER_INSTRUCTIONS = (
    "Initialize Work Buddy with the exact session identity in the user brief, "
    "then complete that brief through Work Buddy capabilities. Treat the "
    "workspace path in the brief as data for Work Buddy, not as permission to "
    "read it directly."
)


def _local_mcp_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("mcp-url must be the local Work Buddy MCP endpoint")
    return value


def build_thread_config(
    *,
    mcp_url: str,
    session_id: str,
    inherited_mcp_server_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Disable inherited MCPs, then register only the local Work Buddy."""

    if not is_safe_session_id(session_id):
        raise ValueError("session_id must be a safe backend identity")

    mcp_servers: dict[str, dict[str, Any]] = {
        name: {"enabled": False}
        for name in inherited_mcp_server_names
    }
    mcp_servers["work-buddy"] = {
        "url": _local_mcp_url(mcp_url),
        "enabled": True,
        "http_headers": {
            "X-Work-Buddy-Session": session_id,
        },
        "default_tools_approval_mode": "approve",
        "tools": {
            "wb_init": {
                "approval_mode": "approve",
            }
        },
    }

    return {"mcp_servers": mcp_servers}


def run_worker(
    *,
    model: str,
    prompt: str,
    session_id: str,
    mcp_url: str,
    codex_factory: Any | None = None,
) -> int:
    """Run one ephemeral read-only Codex thread until the brief completes."""

    if not model or not is_safe_session_id(session_id) or not prompt:
        return 2
    os.environ["WORK_BUDDY_SESSION_ID"] = session_id
    try:
        from openai_codex import ApprovalMode, Codex, Sandbox

        with TemporaryDirectory(
            prefix="work-buddy-codex-host-",
            ignore_cleanup_errors=True,
        ) as host_directory:
            host_cwd = Path(host_directory).resolve()

            if codex_factory is None:
                # The first App Server only reads effective config.  Codex
                # creates MCP connection managers per thread, so no inherited
                # MCP process is started during this discovery pass.
                discovery_factory = lambda: Codex(
                    config=codex_subscription_config(
                        cwd=host_cwd,
                        env=codex_chatgpt_environment(os.environ),
                    )
                )
            else:
                discovery_factory = codex_factory

            with discovery_factory() as discovery:
                discovered_config = read_effective_codex_config(
                    discovery,
                    cwd=host_cwd,
                )
                validate_subscription_codex_config(discovered_config)
                inherited_mcp_server_names = effective_mcp_server_names(
                    discovered_config
                )
                inherited_plugin_ids = effective_plugin_ids(discovered_config)

            launch_isolation = codex_entry_isolation_overrides(
                effective_config=discovered_config,
                plugin_ids=inherited_plugin_ids,
            )
            if codex_factory is None:
                execution_factory = lambda: Codex(
                    config=codex_subscription_config(
                        cwd=host_cwd,
                        env=codex_chatgpt_environment(os.environ),
                        extra_overrides=launch_isolation,
                    )
                )
            else:
                execution_factory = codex_factory

            with execution_factory() as codex:
                effective_config = read_effective_codex_config(
                    codex,
                    cwd=host_cwd,
                )
                validate_subscription_codex_config(effective_config)
                validate_entry_isolation(
                    effective_config,
                    mcp_server_names=inherited_mcp_server_names,
                    plugin_ids=inherited_plugin_ids,
                )
                config = build_thread_config(
                    mcp_url=mcp_url,
                    session_id=session_id,
                    inherited_mcp_server_names=inherited_mcp_server_names,
                )
                thread = codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    base_instructions=_COWORK_BASE_INSTRUCTIONS,
                    config=config,
                    cwd=str(host_cwd),
                    developer_instructions=_COWORK_DEVELOPER_INSTRUCTIONS,
                    ephemeral=True,
                    model=model,
                    model_provider="openai",
                    sandbox=Sandbox.read_only,
                    service_name="work-buddy-cowork",
                )
                result = thread.run(
                    prompt,
                    approval_mode=ApprovalMode.deny_all,
                    model=model,
                    sandbox=Sandbox.read_only,
                )
        logger.info(
            "Codex document worker completed: model=%s, response_len=%d",
            model,
            len(str(getattr(result, "final_response", "") or "")),
        )
        return 0
    except Exception as exc:
        # Do not log exception text: RPC/auth failures can contain account or
        # filesystem details.  Conversation responses never receive this log.
        logger.error(
            "Codex document worker failed: error_type=%s",
            type(exc).__name__,
        )
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--mcp-url", required=True, type=_local_mcp_url)
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
        mcp_url=args.mcp_url,
    )


if __name__ == "__main__":
    raise SystemExit(main())
