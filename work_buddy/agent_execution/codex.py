"""ChatGPT-account-backed Codex execution provider."""

from __future__ import annotations

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

_CODEX_ROUTE_ENV_KEYS = frozenset(
    {
        "codex_api_key",
        "codex_base_url",
        "codex_model",
        "codex_model_provider",
        "codex_provider",
        "openai_api_key",
        "openai_api_base",
        "openai_base_url",
        "openai_model_provider",
        "openai_organization",
        "openai_org_id",
        "openai_project",
        "openai_project_id",
        "openai_provider",
        "azure_openai_api_key",
        "azure_openai_ad_token",
        "azure_openai_api_version",
        "azure_openai_base_url",
        "azure_openai_endpoint",
    }
)
_MAX_PROBE_OUTPUT_CHARS = 256_000
_MAX_EFFECTIVE_NAMED_ENTRIES = 512
_MAX_EFFECTIVE_ENTRY_NAME_CHARS = 512
_CHATGPT_BACKEND_URL = "https://chatgpt.com/backend-api/"
_OPENAI_API_BASE_URL = "https://api.openai.com/v1"
_CODEX_SUBSCRIPTION_CONFIG_OVERRIDES = (
    'forced_login_method="chatgpt"',
    'model_provider="openai"',
    f'chatgpt_base_url="{_CHATGPT_BACKEND_URL}"',
    f'openai_base_url="{_OPENAI_API_BASE_URL}"',
    'approval_policy="never"',
    'sandbox_mode="read-only"',
    "allow_login_shell=false",
    "features.hooks=false",
    "features.apps=false",
    "features.plugins=false",
    "features.plugin_sharing=false",
    "features.remote_plugin=false",
    "features.multi_agent=false",
    "features.shell_tool=false",
    "features.shell_snapshot=false",
    "features.unified_exec=false",
    "features.skill_mcp_dependency_install=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.browser_use_full_cdp_access=false",
    "features.computer_use=false",
    "features.in_app_browser=false",
    "features.code_mode=false",
    "features.code_mode_host=false",
    "features.enable_mcp_apps=false",
    "features.image_generation=false",
    "features.workspace_dependencies=false",
    "features.tool_suggest=false",
    "features.auth_elicitation=false",
    "features.tool_call_mcp_elicitation=false",
    "features.goals=false",
    "features.memories=false",
    "features.external_migration=false",
    "features.mentions_v2=false",
    "features.personality=false",
    "features.realtime_conversation=false",
    "features.artifact=false",
    "features.request_permissions_tool=false",
    "features.remote_control=false",
    "features.network_proxy=false",
    'web_search="disabled"',
    "tools.web_search=false",
    "tools.view_image=false",
    "apps._default.enabled=false",
    "apps._default.destructive_enabled=false",
    "apps._default.open_world_enabled=false",
    "developer_instructions=\"\"",
    "instructions=\"\"",
    "compact_prompt=\"\"",
    "project_doc_max_bytes=0",
    "project_doc_fallback_filenames=[]",
    "include_apps_instructions=false",
    "skills.include_instructions=false",
    "include_environment_context=false",
    "include_permissions_instructions=false",
    "include_collaboration_mode_instructions=false",
    "notify=[]",
    'history.persistence="none"',
    "check_for_update_on_startup=false",
    "feedback.enabled=false",
    "analytics.enabled=false",
    "orchestrator.skills.enabled=false",
    "orchestrator.mcp.enabled=false",
    'otel.exporter="none"',
    'otel.trace_exporter="none"',
    'otel.metrics_exporter="none"',
    "otel.log_user_prompt=false",
)


class CodexConfigIsolationError(RuntimeError):
    """The pinned Codex runtime did not expose a safe effective config."""


def codex_chatgpt_environment(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove API-key, endpoint, and provider overrides.

    The Codex provider is intentionally subscription-only.  Its authority is
    the ChatGPT account in Codex's ``auth.json``; inherited environment
    variables must not silently switch the SDK/App Server to API billing or a
    different endpoint/provider.  ``CODEX_HOME`` remains available so the
    official runtime can find that account.
    """

    source = os.environ if base is None else base
    return {
        key: value
        for key, value in source.items()
        if key.casefold() not in _CODEX_ROUTE_ENV_KEYS
    }


def codex_subscription_config(
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    extra_overrides: tuple[str, ...] = (),
) -> Any:
    """Build the pinned SDK launch config for subscription-only execution."""

    from openai_codex import CodexConfig

    return CodexConfig(
        config_overrides=(
            *_CODEX_SUBSCRIPTION_CONFIG_OVERRIDES,
            *extra_overrides,
        ),
        cwd=str(Path(cwd).resolve()) if cwd is not None else None,
        env=codex_chatgpt_environment(env),
    )


def read_effective_codex_config(
    codex: Any,
    *,
    cwd: str | Path,
) -> dict[str, Any]:
    """Read effective config through the pinned 0.144.x SDK seam.

    The public Python surface does not currently expose ``config/read`` and
    the generated response model omits several host keys, including
    ``mcp_servers``.  Use the pinned client's raw request only after checking
    its shape, and fail closed on any SDK or response-contract drift.
    """

    client = getattr(codex, "_client", None)
    request_raw = getattr(client, "_request_raw", None)
    if not callable(request_raw):
        raise CodexConfigIsolationError("Codex config inspection is unavailable")
    try:
        payload = request_raw(
            "config/read",
            {
                "cwd": str(Path(cwd).resolve()),
                "includeLayers": False,
            },
        )
    except Exception as exc:
        raise CodexConfigIsolationError(
            "Codex config inspection failed"
        ) from exc
    if not isinstance(payload, dict):
        raise CodexConfigIsolationError("Codex config response was invalid")
    config = payload.get("config")
    if not isinstance(config, dict):
        raise CodexConfigIsolationError("Codex effective config was unavailable")
    return config


def validate_subscription_codex_config(config: Mapping[str, Any]) -> None:
    """Fail closed unless the requested minimal ChatGPT route is effective."""

    expected_paths: tuple[tuple[tuple[str, ...], Any], ...] = (
        (("forced_login_method",), "chatgpt"),
        (("model_provider",), "openai"),
        (("chatgpt_base_url",), _CHATGPT_BACKEND_URL),
        (("openai_base_url",), _OPENAI_API_BASE_URL),
        (("approval_policy",), "never"),
        (("sandbox_mode",), "read-only"),
        (("allow_login_shell",), False),
        (("features", "hooks"), False),
        (("features", "apps"), False),
        (("features", "plugins"), False),
        (("features", "plugin_sharing"), False),
        (("features", "remote_plugin"), False),
        (("features", "multi_agent"), False),
        (("features", "shell_tool"), False),
        (("features", "shell_snapshot"), False),
        (("features", "unified_exec"), False),
        (("features", "skill_mcp_dependency_install"), False),
        (("features", "browser_use"), False),
        (("features", "browser_use_external"), False),
        (("features", "browser_use_full_cdp_access"), False),
        (("features", "computer_use"), False),
        (("features", "in_app_browser"), False),
        (("features", "code_mode"), False),
        (("features", "code_mode_host"), False),
        (("features", "enable_mcp_apps"), False),
        (("features", "image_generation"), False),
        (("features", "workspace_dependencies"), False),
        (("features", "tool_suggest"), False),
        (("features", "auth_elicitation"), False),
        (("features", "tool_call_mcp_elicitation"), False),
        (("features", "goals"), False),
        (("features", "memories"), False),
        (("features", "external_migration"), False),
        (("features", "mentions_v2"), False),
        (("features", "personality"), False),
        (("features", "realtime_conversation"), False),
        (("features", "artifact"), False),
        (("features", "request_permissions_tool"), False),
        (("features", "remote_control"), False),
        (("features", "network_proxy"), False),
        (("web_search",), "disabled"),
        (("apps", "_default", "enabled"), False),
        (("apps", "_default", "destructive_enabled"), False),
        (("apps", "_default", "open_world_enabled"), False),
        (("developer_instructions",), ""),
        (("instructions",), ""),
        (("compact_prompt",), ""),
        (("project_doc_max_bytes",), 0),
        (("project_doc_fallback_filenames",), []),
        (("include_apps_instructions",), False),
        (("skills", "include_instructions"), False),
        (("include_environment_context",), False),
        (("include_permissions_instructions",), False),
        (("include_collaboration_mode_instructions",), False),
        (("notify",), []),
        (("history", "persistence"), "none"),
        (("check_for_update_on_startup",), False),
        (("feedback", "enabled"), False),
        (("analytics", "enabled"), False),
        (("orchestrator", "skills", "enabled"), False),
        (("orchestrator", "mcp", "enabled"), False),
        (("otel", "exporter"), "none"),
        (("otel", "trace_exporter"), "none"),
        (("otel", "metrics_exporter"), "none"),
        (("otel", "log_user_prompt"), False),
    )
    for path, expected in expected_paths:
        value: Any = config
        for key in path:
            if not isinstance(value, Mapping) or key not in value:
                raise CodexConfigIsolationError(
                    "Codex effective config did not preserve isolation"
                )
            value = value[key]
        if value != expected:
            raise CodexConfigIsolationError(
                "Codex effective config did not preserve isolation"
            )
    tools = config.get("tools")
    if not isinstance(tools, Mapping):
        raise CodexConfigIsolationError(
            "Codex effective tool config was unavailable"
        )
    if tools.get("web_search") not in (None, False):
        raise CodexConfigIsolationError(
            "Codex web search tool was not isolated"
        )
    if tools.get("view_image") not in (None, False):
        raise CodexConfigIsolationError(
            "Codex image tool was not isolated"
        )
    for path_key in (
        "experimental_compact_prompt_file",
        "experimental_instructions_file",
        "model_catalog_json",
        "model_instructions_file",
    ):
        if config.get(path_key) not in (None, ""):
            raise CodexConfigIsolationError(
                "Codex external model configuration was not isolated"
            )


def _effective_named_table_entries(
    config: Mapping[str, Any],
    *,
    table_name: str,
) -> tuple[str, ...]:
    raw_entries = config.get(table_name)
    if raw_entries is None:
        return ()
    if not isinstance(raw_entries, Mapping):
        raise CodexConfigIsolationError("Codex named config table was invalid")
    if len(raw_entries) > _MAX_EFFECTIVE_NAMED_ENTRIES:
        raise CodexConfigIsolationError("Codex named config table was too large")
    names: list[str] = []
    for raw_name in raw_entries:
        if (
            not isinstance(raw_name, str)
            or not raw_name
            or len(raw_name) > _MAX_EFFECTIVE_ENTRY_NAME_CHARS
        ):
            raise CodexConfigIsolationError("Codex config entry name was invalid")
        names.append(raw_name)
    return tuple(sorted(names))


def effective_mcp_server_names(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Return inherited MCP names from a validated effective config."""

    return _effective_named_table_entries(
        config,
        table_name="mcp_servers",
    )


def effective_plugin_ids(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Return installed/configured plugin ids from effective config."""

    return _effective_named_table_entries(
        config,
        table_name="plugins",
    )


def _toml_key_segment(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def codex_entry_isolation_overrides(
    *,
    effective_config: Mapping[str, Any],
    plugin_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Disable discovered executable integrations before execution startup."""

    raw_servers = effective_config.get("mcp_servers")
    if raw_servers is None:
        raw_servers = {}
    if not isinstance(raw_servers, Mapping):
        raise CodexConfigIsolationError("Codex MCP config was invalid")
    mcp_entries: list[str] = []
    for name in effective_mcp_server_names(effective_config):
        raw_server = raw_servers.get(name)
        if not isinstance(raw_server, Mapping):
            raise CodexConfigIsolationError("Codex MCP transport was invalid")
        command = raw_server.get("command")
        url = raw_server.get("url")
        if isinstance(command, str) and command and url is None:
            # A launch override containing only ``enabled=false`` is rejected
            # by 0.144.x before layers merge because that layer has no
            # transport.  Supply an inert transport of the same kind so the
            # layer validates, while ensuring the inherited executable and
            # arguments cannot be selected even if merge behavior changes.
            inert_command = _toml_key_segment(sys.executable)
            value = (
                f'{{command={inert_command},args=["-c","pass"],enabled=false}}'
            )
        elif isinstance(url, str) and url and command is None:
            value = (
                '{url="http://127.0.0.1:9/work-buddy-disabled",'
                "enabled=false}"
            )
        else:
            raise CodexConfigIsolationError("Codex MCP transport was invalid")
        mcp_entries.append(f"{_toml_key_segment(name)}={value}")

    overrides: list[str] = []
    if mcp_entries:
        # The 0.144.x CLI override parser treats quotes in dotted key paths as
        # literal key characters.  Put quoted names inside one inline table so
        # punctuation-bearing MCP names address the inherited entries exactly.
        overrides.append(f"mcp_servers={{{','.join(mcp_entries)}}}")
    if plugin_ids:
        plugin_entries = ",".join(
            f"{_toml_key_segment(plugin_id)}={{enabled=false}}"
            for plugin_id in plugin_ids
        )
        overrides.append(f"plugins={{{plugin_entries}}}")
    return tuple(overrides)


def validate_entry_isolation(
    config: Mapping[str, Any],
    *,
    mcp_server_names: tuple[str, ...],
    plugin_ids: tuple[str, ...],
) -> None:
    """Verify every discovered executable integration is launch-disabled."""

    expected_tables = (
        ("mcp_servers", tuple(sorted(mcp_server_names))),
        ("plugins", tuple(sorted(plugin_ids))),
    )
    actual_tables = (
        ("mcp_servers", effective_mcp_server_names(config)),
        ("plugins", effective_plugin_ids(config)),
    )
    if actual_tables != expected_tables:
        raise CodexConfigIsolationError(
            "Codex executable integration config changed during isolation"
        )

    for table_name, names in expected_tables:
        if not names:
            continue
        table = config.get(table_name)
        if not isinstance(table, Mapping):
            raise CodexConfigIsolationError(
                "Codex executable integration config was unavailable"
            )
        for name in names:
            entry = table.get(name)
            if not isinstance(entry, Mapping) or entry.get("enabled") is not False:
                raise CodexConfigIsolationError(
                    "Codex executable integration was not isolated"
                )


def _mcp_endpoint() -> str:
    cfg = load_config()
    port = (
        cfg.get("sidecar", {})
        .get("services", {})
        .get("mcp_gateway", {})
        .get("port", 5126)
    )
    return f"http://localhost:{int(port)}/mcp"


class CodexProvider:
    """Official Python SDK plus its pinned local Codex runtime."""

    provider_id = "codex"
    label = "Codex"
    auth_mode = "chatgpt"
    default_model_id = ""

    def __init__(
        self,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 12.0,
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
        try:
            from work_buddy.compat import subprocess_creation_flags

            result = self._run(
                [
                    sys.executable,
                    "-m",
                    "work_buddy.agent_execution.codex_probe_worker",
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
                env=codex_chatgpt_environment(),
                creationflags=subprocess_creation_flags(),
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return self._unavailable(
                ProviderAvailability.UNKNOWN,
                "Codex didn't respond in time.",
            )
        except (FileNotFoundError, PermissionError, OSError):
            return self._unavailable(
                ProviderAvailability.UNAVAILABLE,
                "Codex support couldn't be opened.",
            )

        stdout = (result.stdout or "")[:_MAX_PROBE_OUTPUT_CHARS]
        if result.returncode != 0 or not stdout:
            return self._unavailable(
                ProviderAvailability.UNKNOWN,
                "Codex couldn't be checked.",
            )
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError):
            return self._unavailable(
                ProviderAvailability.UNKNOWN,
                "Codex couldn't be checked.",
            )
        if not isinstance(payload, dict):
            return self._unavailable(
                ProviderAvailability.UNKNOWN,
                "Codex couldn't be checked.",
            )
        return self._descriptor_from_redacted_payload(payload)

    def _descriptor_from_redacted_payload(
        self,
        payload: dict[str, Any],
    ) -> ProviderDescriptor:
        try:
            availability = ProviderAvailability(
                str(payload.get("availability") or "unknown")
            )
        except ValueError:
            availability = ProviderAvailability.UNKNOWN

        models: list[ModelDescriptor] = []
        raw_models = payload.get("models")
        if isinstance(raw_models, list):
            seen: set[str] = set()
            for raw in raw_models:
                if not isinstance(raw, dict):
                    continue
                model_id = str(raw.get("id") or "").strip()
                label = str(raw.get("label") or model_id).strip()
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                models.append(
                    ModelDescriptor(
                        id=model_id,
                        label=label,
                        available=(
                            availability is ProviderAvailability.READY
                            and bool(raw.get("available", True))
                        ),
                        description=str(raw.get("description") or ""),
                        unavailable_reason=str(
                            raw.get("unavailable_reason") or ""
                        ),
                        is_default=bool(raw.get("is_default", False)),
                    )
                )

        reason = str(payload.get("unavailable_reason") or "")
        if availability is ProviderAvailability.READY and not models:
            availability = ProviderAvailability.UNAVAILABLE
            reason = "Codex didn't report any available models."
        return ProviderDescriptor(
            id=self.provider_id,
            label=self.label,
            availability=availability,
            auth_mode=self.auth_mode,
            models=tuple(models),
            description="Uses your signed-in ChatGPT account.",
            unavailable_reason=reason,
            state_key=str(payload.get("state_key") or ""),
        )

    def _unavailable(
        self,
        availability: ProviderAvailability,
        reason: str,
    ) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=self.provider_id,
            label=self.label,
            availability=availability,
            auth_mode=self.auth_mode,
            description="Uses your signed-in ChatGPT account.",
            unavailable_reason=reason,
        )

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
                descriptor.unavailable_reason or "Codex is unavailable."
            )
        model = next(
            (item for item in descriptor.models if item.id == selection.model_id),
            None,
        )
        if model is None or not model.available:
            raise UnknownModelError(
                f"Model '{selection.model_id}' is not available for Codex."
            )
        return AgentExecutionSelection(
            provider_id=self.provider_id,
            model_id=model.id,
            provider_label=self.label,
            model_label=model.label,
        )

    def start_detached(self, request: AgentSpawnRequest) -> AgentSpawnOutcome:
        selection = self.validate_selection(request.selection)
        session_id = request.session_id

        from work_buddy.sidecar.dispatch.executor import (
            spawn_detached_process_authorized,
        )

        argv = [
            sys.executable,
            "-m",
            "work_buddy.agent_execution.codex_worker",
            "--model",
            selection.model_id,
            "--session-id",
            session_id,
            "--mcp-url",
            _mcp_endpoint(),
        ]
        child_env = codex_chatgpt_environment()
        child_env["WORK_BUDDY_SESSION_ID"] = session_id
        result = spawn_detached_process_authorized(
            name=request.name,
            argv=argv,
            cwd=Path(__file__).resolve().parents[2],
            env=child_env,
            stdin_text=prompt_with_execution_identity(
                request,
                harness_id="codexcli",
            ),
            session_name=session_id,
            missing_executable_error="Codex support is not installed.",
            spawn_error="Codex couldn't start.",
        )
        return AgentSpawnOutcome(
            status=str(result.get("status") or "error"),
            selection=selection,
            pid=result.get("pid") if isinstance(result.get("pid"), int) else None,
            session_id=session_id,
            error_code=str(result.get("error_code") or ""),
            error=str(result.get("error") or ""),
        )
