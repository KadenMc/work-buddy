"""Claude Code account-backed execution provider."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from work_buddy.config import load_config

from .cache import ProbeCache
from .identity import prompt_with_execution_identity
from .models import (
    AgentExecutionSelection,
    AgentSpawnOutcome,
    AgentSpawnRequest,
    ModelDescriptor,
    ProviderAvailability,
    ProviderDescriptor,
    ProviderUnavailableError,
    UnknownModelError,
)

_CLAUDE_ACCOUNT_ROUTING_ENV_KEYS = frozenset(
    {
        "aws_bearer_token_bedrock",
        "claude_bg_auth_snapshot_path",
        "claude_code_api_base_url",
        "claude_code_api_key_file_descriptor",
        "claude_code_api_key_helper_ttl_ms",
        "claude_code_assume_first_party_base_url",
        "claude_code_custom_oauth_url",
        "claude_code_force_windows_credman",
        "claude_code_host_auth_env_var",
        "claude_code_host_creds_file",
        "claude_code_managed_settings_path",
        "claude_code_provider_managed_by_host",
        "claude_config_dir",
    }
)

_CLAUDE_ACCOUNT_ROUTING_ENV_PREFIXES = (
    "anthropic_",
    "subagent_anthropic_",
    "claude_code_oauth_",
    "claude_code_skip_",
    "claude_code_use_",
)

_CLAUDE_TELEMETRY_ENV_KEYS = frozenset(
    {
        "claude_code_enable_telemetry",
        "claude_code_enhanced_telemetry_beta",
        "claude_code_disable_nonessential_traffic",
        "enable_enhanced_telemetry_beta",
        "claude_code_otel_content_max_length",
        "claude_code_otel_headers_helper_debounce_ms",
        "claude_code_propagate_traceparent",
        "disable_telemetry",
        "disable_error_reporting",
        "do_not_track",
        "traceparent",
        "tracestate",
    }
)

_CLAUDE_DEBUG_ENV_KEYS = frozenset(
    {
        "claude_debug",
        "debug",
        "debug_claude_agent_sdk",
        "debug_sdk",
    }
)

_CLAUDE_CUSTOMIZATION_ENV_KEYS = frozenset(
    {
        "claude_code_disable_official_marketplace_autoinstall",
        "claude_code_enable_background_plugin_refresh",
        "claude_code_sync_plugin_install",
        "claude_code_sync_plugins",
        "claude_code_sync_skills",
        "force_autoupdate_plugins",
    }
)

_CLAUDE_IDE_ENV_KEYS = frozenset(
    {
        "claude_code_auto_connect_ide",
        "claude_code_disable_attachments",
        "claude_code_disable_auto_memory",
        "claude_code_disable_claude_mds",
        "claude_code_ide_skip_auto_install",
    }
)

_CLAUDE_MODELS = (
    ModelDescriptor(
        id="sonnet",
        label="Sonnet",
        description="Balanced Claude Code model for everyday document work.",
        is_default=True,
    ),
    ModelDescriptor(
        id="opus",
        label="Opus",
        description="Most capable Claude Code model for demanding review work.",
    ),
)


def claude_account_environment(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment pinned to local Claude-account execution.

    Claude account authentication is read from Claude Code's normal secure
    credential store. No inherited Anthropic credential or routing variable is
    needed, so all such variables are removed instead of attempting to predict
    which future names may affect billing or provider selection.
    """

    source = os.environ if base is None else base
    sanitized = {
        key: value
        for key, value in source.items()
        if (
            key.casefold() not in _CLAUDE_ACCOUNT_ROUTING_ENV_KEYS
            and key.casefold() not in _CLAUDE_TELEMETRY_ENV_KEYS
            and key.casefold() not in _CLAUDE_IDE_ENV_KEYS
            and key.casefold() not in _CLAUDE_DEBUG_ENV_KEYS
            and key.casefold() not in _CLAUDE_CUSTOMIZATION_ENV_KEYS
            and not key.casefold().startswith(
                _CLAUDE_ACCOUNT_ROUTING_ENV_PREFIXES
            )
            and not key.casefold().startswith("claude_code_debug_")
            and not key.casefold().startswith("otel_")
        )
    }
    # Claude Code requires this flag before it collects or exports OTel data.
    # Pinning it off protects the worker even when the parent environment used
    # oddly-cased variables that were stripped above.
    sanitized["CLAUDE_CODE_ENABLE_TELEMETRY"] = "0"
    sanitized["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "0"
    sanitized["ENABLE_ENHANCED_TELEMETRY_BETA"] = "0"
    sanitized["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    sanitized["CLAUDE_CODE_AUTO_CONNECT_IDE"] = "false"
    sanitized["CLAUDE_CODE_DISABLE_ATTACHMENTS"] = "1"
    sanitized["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    sanitized["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    sanitized["CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL"] = "1"
    sanitized["CLAUDE_CODE_SYNC_PLUGINS"] = "0"
    sanitized["CLAUDE_CODE_SYNC_PLUGIN_INSTALL"] = "0"
    sanitized["CLAUDE_CODE_SYNC_SKILLS"] = "0"
    sanitized["CLAUDE_CODE_ENABLE_BACKGROUND_PLUGIN_REFRESH"] = "0"
    sanitized["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] = "1"
    return sanitized


def _safe_state_key(
    availability: ProviderAvailability,
    auth_mode: str,
    model_ids: tuple[str, ...],
) -> str:
    material = "\0".join(
        ("claude-code", availability.value, auth_mode, *model_ids)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _models_for_probe(
    availability: ProviderAvailability,
    unavailable_reason: str,
) -> tuple[ModelDescriptor, ...]:
    ready = availability is ProviderAvailability.READY
    return tuple(
        ModelDescriptor(
            id=model.id,
            label=model.label,
            available=ready,
            description=model.description,
            unavailable_reason="" if ready else unavailable_reason,
            is_default=model.is_default,
        )
        for model in _CLAUDE_MODELS
    )


def _isolated_setting_args() -> tuple[str, ...]:
    """Ignore user, project, and local settings for this invocation.

    Claude Code can still enforce administrator-managed policy settings. Those
    settings are part of the host's administrative trust boundary and cannot
    be bypassed by this process-level isolation.
    """

    return (
        "--setting-sources",
        "",
        "--settings",
        "{}",
    )


def _isolated_worker_setting_args() -> tuple[str, ...]:
    """Isolate a headless worker without hiding its explicit MCP config.

    The Claude Code CLI suppresses ``--mcp-config`` when the invocation also
    supplies an empty ``--setting-sources`` value.  The document worker
    therefore pairs a credential-only config directory with one explicit
    settings overlay: it disables extension surfaces and narrowly approves
    only the server supplied by its strict, server-authored MCP configuration.
    """

    settings = {
        "autoMemoryEnabled": False,
        "disableAllHooks": True,
        "disableArtifact": True,
        "disableBundledSkills": True,
        "disableClaudeAiConnectors": True,
        "disableWorkflows": True,
        "enabledMcpjsonServers": ["work-buddy"],
    }
    return (
        "--settings",
        json.dumps(settings, separators=(",", ":"), ensure_ascii=True),
    )


def _work_buddy_mcp_config(session_id: str) -> str:
    cfg = load_config()
    port = (
        cfg.get("sidecar", {})
        .get("services", {})
        .get("mcp_gateway", {})
        .get("port", 5126)
    )
    payload = {
        "mcpServers": {
            "work-buddy": {
                "type": "http",
                "url": f"http://localhost:{int(port)}/mcp",
                "headers": {
                    "X-Work-Buddy-Session": session_id,
                },
            }
        }
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


class ClaudeCodeProvider:
    """Local Claude CLI authenticated through the user's Claude account."""

    provider_id = "claude-code"
    label = "Claude Code"
    auth_mode = "claude_account"
    default_model_id = "sonnet"

    def __init__(
        self,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 15.0,
        cache: ProbeCache[ProviderDescriptor] | None = None,
    ) -> None:
        self._run = command_runner
        self._timeout_seconds = timeout_seconds
        self._cache = cache or ProbeCache(ttl_seconds=30.0)

    def probe(self, *, refresh: bool = False) -> ProviderDescriptor:
        return self._cache.get_or_load(
            self.provider_id,
            self._probe_uncached,
            refresh=refresh,
        )

    def clear_probe_cache(self) -> None:
        self._cache.clear(self.provider_id)

    def _probe_uncached(self) -> ProviderDescriptor:
        availability = ProviderAvailability.UNKNOWN
        reason = "Claude Code couldn't be checked."
        try:
            from work_buddy.compat import subprocess_creation_flags

            result = self._run(
                [
                    "claude",
                    *_isolated_setting_args(),
                    "auth",
                    "status",
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
                env=claude_account_environment(),
                creationflags=subprocess_creation_flags(),
                shell=False,
            )
        except FileNotFoundError:
            availability = ProviderAvailability.UNAVAILABLE
            reason = "Claude Code is not installed."
        except subprocess.TimeoutExpired:
            availability = ProviderAvailability.UNKNOWN
            reason = "Claude Code didn't respond in time."
        except (PermissionError, OSError):
            availability = ProviderAvailability.UNAVAILABLE
            reason = "Claude Code couldn't be opened."
        else:
            availability, reason = self._classify_auth_status(result)

        models = _models_for_probe(availability, reason)
        return ProviderDescriptor(
            id=self.provider_id,
            label=self.label,
            availability=availability,
            auth_mode=self.auth_mode,
            models=models,
            description="Uses your signed-in Claude account.",
            unavailable_reason="" if availability is ProviderAvailability.READY else reason,
            state_key=_safe_state_key(
                availability,
                self.auth_mode,
                tuple(model.id for model in models),
            ),
        )

    @staticmethod
    def _classify_auth_status(
        result: subprocess.CompletedProcess[str],
    ) -> tuple[ProviderAvailability, str]:
        output = (result.stdout or "").strip()
        payload: dict[str, Any] | None = None
        if output:
            try:
                candidate = json.loads(output)
                if isinstance(candidate, dict):
                    payload = candidate
            except (TypeError, ValueError):
                payload = None

        if payload is not None:
            logged_in = payload.get("loggedIn", payload.get("logged_in"))
            if logged_in is True:
                auth_method = str(
                    payload.get("authMethod", payload.get("auth_method", ""))
                    or ""
                ).casefold()
                api_provider = str(
                    payload.get("apiProvider", payload.get("api_provider", ""))
                    or ""
                ).casefold()
                subscription_type = str(
                    payload.get(
                        "subscriptionType",
                        payload.get("subscription_type", ""),
                    )
                    or ""
                ).strip()
                if (
                    auth_method == "claude.ai"
                    and api_provider == "firstparty"
                    and subscription_type
                ):
                    return ProviderAvailability.READY, ""
                return (
                    ProviderAvailability.UNAVAILABLE,
                    "Sign in to Claude Code with your Claude account.",
                )
            if logged_in is False:
                return (
                    ProviderAvailability.AUTH_REQUIRED,
                    "Sign in to Claude Code with your Claude account.",
                )

        combined = f"{result.stdout or ''}\n{result.stderr or ''}".casefold()
        if (
            "not logged in" in combined
            or "not authenticated" in combined
            or "sign in" in combined
            or "login" in combined
        ):
            return (
                ProviderAvailability.AUTH_REQUIRED,
                "Sign in to Claude Code with your Claude account.",
            )
        return ProviderAvailability.UNKNOWN, "Claude Code couldn't be checked."

    def validate_selection(
        self,
        selection: AgentExecutionSelection,
        *,
        refresh: bool = False,
    ) -> AgentExecutionSelection:
        if selection.provider_id != self.provider_id:
            raise ValueError("Selection belongs to a different provider")
        descriptor = self.probe(refresh=refresh)
        if not descriptor.available:
            raise ProviderUnavailableError(
                descriptor.unavailable_reason or "Claude Code is unavailable."
            )
        model = next(
            (item for item in descriptor.models if item.id == selection.model_id),
            None,
        )
        if model is None or not model.available:
            raise UnknownModelError(
                f"Model '{selection.model_id}' is not available for Claude Code."
            )
        return AgentExecutionSelection(
            provider_id=self.provider_id,
            model_id=model.id,
            provider_label=self.label,
            model_label=model.label,
        )

    def start_detached(self, request: AgentSpawnRequest) -> AgentSpawnOutcome:
        """Start Claude with the exact validated alias and account-only env."""

        selection = self.validate_selection(request.selection)
        session_id = request.session_id
        cfg = load_config()
        agent_cfg = cfg.get("sidecar", {}).get("agent_spawn", {})
        budget = (
            request.max_budget_usd
            if request.max_budget_usd is not None
            else float(agent_cfg.get("max_budget_usd", 1.0))
        )
        process_owner = session_id

        from work_buddy.sidecar.dispatch.executor import (
            spawn_detached_process_authorized,
        )

        argv = [
            sys.executable,
            "-m",
            "work_buddy.agent_execution.claude_worker",
            "--model",
            selection.model_id,
            "--session-id",
            session_id,
            "--max-budget-usd",
            str(budget),
        ]
        source_config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        child_env = claude_account_environment()
        if source_config_dir:
            # The trusted wrapper uses this only to seed its credential-only
            # config directory; it never forwards the source directory to
            # Claude itself.
            child_env["CLAUDE_CONFIG_DIR"] = source_config_dir
        child_env["WORK_BUDDY_SESSION_ID"] = session_id
        result = spawn_detached_process_authorized(
            name=request.name,
            argv=argv,
            cwd=Path(__file__).resolve().parents[2],
            env=child_env,
            stdin_text=prompt_with_execution_identity(
                request,
                harness_id="claudecode",
            ),
            session_name=process_owner,
            missing_executable_error="Claude Code is not installed.",
            spawn_error="Claude Code couldn't start.",
        )
        return AgentSpawnOutcome(
            status=str(result.get("status") or "error"),
            selection=selection,
            pid=result.get("pid") if isinstance(result.get("pid"), int) else None,
            session_id=session_id,
            error_code=str(result.get("error_code") or ""),
            error=str(result.get("error") or ""),
        )
