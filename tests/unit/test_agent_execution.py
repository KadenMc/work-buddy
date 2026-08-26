from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from work_buddy.agent_execution import registry as execution_registry
from work_buddy.agent_execution.cache import ProbeCache
from work_buddy.agent_execution.claude_code import (
    ClaudeCodeProvider,
    claude_account_environment,
)
from work_buddy.agent_execution.claude_worker import (
    _claude_config_source,
    _isolated_claude_config,
    _retry_cleanup,
    run_worker as run_claude_worker,
)
from work_buddy.agent_execution.identity import prompt_with_execution_identity
from work_buddy.agent_execution.codex import (
    CodexConfigIsolationError,
    CodexProvider,
    codex_chatgpt_environment,
    codex_entry_isolation_overrides,
    codex_subscription_config,
    effective_mcp_server_names,
    effective_plugin_ids,
    read_effective_codex_config,
    validate_entry_isolation,
    validate_subscription_codex_config,
)
from work_buddy.agent_execution.codex_probe_worker import collect_redacted_probe
from work_buddy.agent_execution.codex_worker import (
    _local_mcp_url,
    build_thread_config,
    run_worker,
)
from work_buddy.agent_execution.models import (
    AgentExecutionSelection,
    AgentSpawnRequest,
    ModelDescriptor,
    ProviderAvailability,
    ProviderDescriptor,
    ProviderUnavailableError,
    UnknownModelError,
    UnknownProviderError,
)
from work_buddy.agent_execution.registry import ProviderRegistry


def test_claude_execution_identity_uses_only_toolsearch_for_bootstrap(
    tmp_path: Path,
) -> None:
    request = AgentSpawnRequest(
        name="cowork-document",
        prompt="Handle the durable inbox.",
        selection=AgentExecutionSelection("claude-code", "sonnet"),
        session_id="cowork-generation-a",
        working_directory=tmp_path,
    )

    claude_prompt = prompt_with_execution_identity(
        request,
        harness_id="claudecode",
    )
    codex_prompt = prompt_with_execution_identity(
        request,
        harness_id="codexcli",
    )

    assert "Only Claude Code's ToolSearch is initially available" in claude_prompt
    assert "`mcp__work-buddy__wb_init`" in claude_prompt
    assert "`mcp__work-buddy__wb_search`" in claude_prompt
    assert "Do not load or use any non-Work-Buddy tool" in claude_prompt
    assert "session_id=\"cowork-generation-a\"" in claude_prompt
    assert "harness_id=\"claudecode\"" in claude_prompt
    assert "ToolSearch" not in codex_prompt


def _isolated_effective_codex_config(
    *mcp_server_names: str,
    plugin_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "forced_login_method": "chatgpt",
        "model_provider": "openai",
        "chatgpt_base_url": "https://chatgpt.com/backend-api/",
        "openai_base_url": "https://api.openai.com/v1",
        "approval_policy": "never",
        "sandbox_mode": "read-only",
        "allow_login_shell": False,
        "features": {
            "hooks": False,
            "apps": False,
            "plugins": False,
            "plugin_sharing": False,
            "remote_plugin": False,
            "multi_agent": False,
            "shell_tool": False,
            "shell_snapshot": False,
            "unified_exec": False,
            "skill_mcp_dependency_install": False,
            "browser_use": False,
            "browser_use_external": False,
            "browser_use_full_cdp_access": False,
            "computer_use": False,
            "in_app_browser": False,
            "code_mode": False,
            "code_mode_host": False,
            "enable_mcp_apps": False,
            "image_generation": False,
            "workspace_dependencies": False,
            "tool_suggest": False,
            "auth_elicitation": False,
            "tool_call_mcp_elicitation": False,
            "goals": False,
            "memories": False,
            "external_migration": False,
            "mentions_v2": False,
            "personality": False,
            "realtime_conversation": False,
            "artifact": False,
            "request_permissions_tool": False,
            "remote_control": False,
            "network_proxy": False,
        },
        "web_search": "disabled",
        "tools": {
            "web_search": None,
            "view_image": None,
        },
        "apps": {
            "_default": {
                "enabled": False,
                "destructive_enabled": False,
                "open_world_enabled": False,
            }
        },
        "developer_instructions": "",
        "instructions": "",
        "compact_prompt": "",
        "project_doc_max_bytes": 0,
        "project_doc_fallback_filenames": [],
        "include_apps_instructions": False,
        "skills": {"include_instructions": False},
        "include_environment_context": False,
        "include_permissions_instructions": False,
        "include_collaboration_mode_instructions": False,
        "notify": [],
        "history": {"persistence": "none"},
        "check_for_update_on_startup": False,
        "feedback": {"enabled": False},
        "analytics": {"enabled": False},
        "orchestrator": {
            "skills": {"enabled": False},
            "mcp": {"enabled": False},
        },
        "otel": {
            "exporter": "none",
            "trace_exporter": "none",
            "metrics_exporter": "none",
            "log_user_prompt": False,
        },
        "mcp_servers": {
            name: {
                "command": sys.executable,
                "args": ["-m", "example"],
                "enabled": True,
            }
            for name in mcp_server_names
        },
        "plugins": {
            plugin_id: {"enabled": True}
            for plugin_id in plugin_ids
        },
    }


def test_selection_has_stable_json_projection() -> None:
    selection = AgentExecutionSelection(
        provider_id="codex",
        model_id="gpt-test",
        provider_label="Codex",
        model_label="GPT Test",
    )

    assert selection.to_dict() == {
        "provider_id": "codex",
        "model_id": "gpt-test",
        "provider_label": "Codex",
        "model_label": "GPT Test",
    }


def test_probe_cache_reuses_then_refreshes() -> None:
    now = [100.0]
    calls: list[int] = []
    cache: ProbeCache[str] = ProbeCache(
        ttl_seconds=30,
        clock=lambda: now[0],
    )

    def load() -> str:
        calls.append(1)
        return f"value-{len(calls)}"

    assert cache.get_or_load("provider", load) == "value-1"
    assert cache.get_or_load("provider", load) == "value-1"
    assert cache.get_or_load("provider", load, refresh=True) == "value-2"
    now[0] = 131.0
    assert cache.get_or_load("provider", load) == "value-3"


def test_claude_probe_is_cached_and_strips_api_billing_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-pass")
    monkeypatch.setenv("SubAgent_Anthropic_Api_Key", "do-not-pass-either")
    calls: list[dict[str, object]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"loggedIn":true,"authMethod":"claude.ai",'
                '"apiProvider":"firstParty","subscriptionType":"max",'
                '"email":"private@example.test"}'
            ),
            stderr="",
        )

    provider = ClaudeCodeProvider(command_runner=run)
    first = provider.probe()
    second = provider.probe()

    assert first is second
    assert first.availability is ProviderAvailability.READY
    assert [model.id for model in first.models] == ["sonnet", "opus"]
    assert len(calls) == 1
    assert calls[0]["command"] == [
        "claude",
        "--setting-sources",
        "",
        "--settings",
        "{}",
        "auth",
        "status",
    ]
    assert calls[0]["shell"] is False
    child_env = calls[0]["env"]
    assert isinstance(child_env, dict)
    assert all(
        key.casefold()
        not in {"anthropic_api_key", "subagent_anthropic_api_key"}
        for key in child_env
    )
    assert "private@example.test" not in json.dumps(first.to_dict())


def test_claude_probe_auth_required_and_allowlist_validation() -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout='{"loggedIn":false}',
            stderr="",
        )

    provider = ClaudeCodeProvider(command_runner=run)
    descriptor = provider.probe()

    assert descriptor.availability is ProviderAvailability.AUTH_REQUIRED
    assert descriptor.unavailable_reason == (
        "Sign in to Claude Code with your Claude account."
    )
    assert all(not model.available for model in descriptor.models)
    with pytest.raises(ProviderUnavailableError):
        provider.validate_selection(
            AgentExecutionSelection("claude-code", "sonnet")
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "loggedIn": True,
            "authMethod": "api_key",
            "apiProvider": "firstParty",
            "subscriptionType": "",
            "email": "private@example.test",
        },
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "bedrock",
            "subscriptionType": "max",
            "orgName": "Private Organization",
        },
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "",
        },
    ],
)
def test_claude_probe_rejects_non_subscription_auth_without_identity(
    payload: dict[str, object],
) -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    descriptor = ClaudeCodeProvider(command_runner=run).probe()

    assert descriptor.availability is ProviderAvailability.UNAVAILABLE
    assert descriptor.unavailable_reason == (
        "Sign in to Claude Code with your Claude account."
    )
    public = json.dumps(descriptor.to_dict())
    assert "private@example.test" not in public
    assert "Private Organization" not in public


def test_claude_probe_never_trusts_non_json_logged_in_text() -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Logged in and authenticated with some credential.",
            stderr="",
        )

    descriptor = ClaudeCodeProvider(command_runner=run).probe()

    assert descriptor.availability is ProviderAvailability.UNKNOWN
    assert descriptor.available is False


def test_claude_rejects_model_outside_allowlist() -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"loggedIn":true,"authMethod":"claude.ai",'
                '"apiProvider":"firstParty","subscriptionType":"max"}'
            ),
            stderr="",
        )

    provider = ClaudeCodeProvider(command_runner=run)
    with pytest.raises(UnknownModelError):
        provider.validate_selection(
            AgentExecutionSelection("claude-code", "haiku")
        )


def test_claude_start_passes_exact_model_and_account_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-in-child")
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "inherited-bootstrap-id")
    source_config = tmp_path / "claude-source"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(source_config))

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"loggedIn":true,"authMethod":"claude.ai",'
                '"apiProvider":"firstParty","subscriptionType":"max"}'
            ),
            stderr="",
        )

    captured: dict[str, object] = {}

    def spawn(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "ok", "pid": 42}

    monkeypatch.setattr(
        "work_buddy.sidecar.dispatch.executor.spawn_detached_process_authorized",
        spawn,
    )
    provider = ClaudeCodeProvider(command_runner=run)
    outcome = provider.start_detached(
        AgentSpawnRequest(
            name="document",
            prompt="brief",
            selection=AgentExecutionSelection("claude-code", "opus"),
            working_directory=tmp_path,
            session_id="claude-generation-cowork",
            max_budget_usd=3.5,
        )
    )

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[:3] == [
        sys.executable,
        "-m",
        "work_buddy.agent_execution.claude_worker",
    ]
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--max-budget-usd") + 1] == "3.5"
    assert captured["session_name"] == "claude-generation-cowork"
    assert "brief" not in argv
    stdin_text = captured["stdin_text"]
    assert isinstance(stdin_text, str)
    assert '`session_id="claude-generation-cowork"`' in stdin_text
    assert '`harness_id="claudecode"`' in stdin_text
    assert "`workspace=" not in stdin_text
    assert str(tmp_path.resolve()) not in stdin_text
    assert "inherited-bootstrap-id" not in stdin_text
    assert stdin_text.endswith("brief")
    assert Path(captured["cwd"]) != tmp_path.resolve()
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "ANTHROPIC_API_KEY" not in child_env
    assert child_env["CLAUDE_CONFIG_DIR"] == str(source_config)
    assert (
        child_env["WORK_BUDDY_SESSION_ID"]
        == "claude-generation-cowork"
    )
    assert outcome.ok and outcome.pid == 42
    assert outcome.session_id == "claude-generation-cowork"
    assert outcome.selection.model_label == "Opus"


def test_claude_worker_uses_empty_neutral_cwd_and_no_session_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-in-claude")
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "inherited-bootstrap-id")
    source_config = tmp_path / "claude-source"
    source_config.mkdir()
    credentials = source_config / ".credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "claudeAiOauth": {"accessToken": "account-backed"},
                "mcpOAuth": {
                    "unrelated-server": {"accessToken": "do-not-copy"}
                },
            }
        ),
        encoding="utf-8",
    )
    (source_config / "settings.json").write_text(
        '{"apiKeyHelper":"do-not-load"}',
        encoding="utf-8",
    )
    (source_config / "CLAUDE.md").write_text(
        "Do not load this instruction.",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(source_config))
    calls: dict[str, object] = {}
    isolated_config_paths: list[Path] = []

    def run(command: list[str], **kwargs: object) -> object:
        calls["command"] = command
        calls.update(kwargs)
        host_cwd = Path(kwargs["cwd"])
        assert host_cwd.is_dir()
        assert list(host_cwd.iterdir()) == []
        assert host_cwd != tmp_path.resolve()
        child_env = kwargs["env"]
        assert isinstance(child_env, dict)
        isolated_config = Path(child_env["CLAUDE_CONFIG_DIR"])
        isolated_config_paths.append(isolated_config)
        assert isolated_config != source_config.resolve()
        assert sorted(path.name for path in isolated_config.iterdir()) == [
            ".credentials.json"
        ]
        assert not (isolated_config / "settings.json").exists()
        assert not (isolated_config / "CLAUDE.md").exists()
        isolated_credentials = json.loads(
            (isolated_config / ".credentials.json").read_text(
                encoding="utf-8"
            )
        )
        assert isolated_credentials == {
            "claudeAiOauth": {"accessToken": "account-backed"}
        }
        assert "mcpOAuth" not in isolated_credentials
        return SimpleNamespace(returncode=0)

    result = run_claude_worker(
        model="opus",
        prompt="private brief",
        session_id="claude-generation-cowork",
        max_budget_usd=3.5,
        command_runner=run,
    )

    assert result == 0
    argv = calls["command"]
    assert isinstance(argv, list)
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--max-budget-usd") + 1] == "3.5"
    assert "--setting-sources" not in argv
    assert json.loads(argv[argv.index("--settings") + 1]) == {
        "autoMemoryEnabled": False,
        "disableAllHooks": True,
        "disableArtifact": True,
        "disableBundledSkills": True,
        "disableClaudeAiConnectors": True,
        "disableWorkflows": True,
        "enabledMcpjsonServers": ["work-buddy"],
    }
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert "--no-chrome" in argv
    assert argv.count("--disable-slash-commands") == 1
    assert argv[argv.index("--tools") + 1] == "ToolSearch"
    assert "--bare" not in argv
    assert "private brief" not in argv
    mcp_config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert mcp_config == {
        "mcpServers": {
            "work-buddy": {
                "type": "http",
                "url": "http://localhost:5126/mcp",
                "headers": {
                    "X-Work-Buddy-Session": "claude-generation-cowork",
                },
            }
        }
    }
    assert calls["input"] == "private brief"
    assert calls["shell"] is False
    assert isinstance(calls["stdout"], int)
    assert calls["stdout"] >= 0
    assert calls["stderr"] is subprocess.DEVNULL
    assert calls["encoding"] == "utf-8"
    assert calls["errors"] == "strict"
    assert not Path(calls["cwd"]).exists()
    assert len(isolated_config_paths) == 1
    assert not isolated_config_paths[0].exists()
    child_env = calls["env"]
    assert isinstance(child_env, dict)
    assert "ANTHROPIC_API_KEY" not in child_env
    assert child_env["CLAUDE_CODE_DISABLE_ATTACHMENTS"] == "1"
    assert child_env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert child_env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert child_env["CLAUDE_CODE_AUTO_CONNECT_IDE"] == "false"
    assert child_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert (
        child_env["WORK_BUDDY_SESSION_ID"]
        == "claude-generation-cowork"
    )


def test_claude_config_source_defaults_to_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "work_buddy.agent_execution.claude_worker.Path.home",
        lambda: tmp_path,
    )

    assert _claude_config_source({}) == (tmp_path / ".claude").resolve()


def test_claude_config_projection_is_private_and_ephemeral(
    tmp_path: Path,
) -> None:
    source_config = tmp_path / "claude-source"
    source_config.mkdir()
    source_credentials = source_config / ".credentials.json"
    source_credentials.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "private-token",
                    "refreshToken": "private-refresh",
                },
                "mcpOAuth": {
                    "unrelated-server": {"accessToken": "other-token"}
                },
            }
        ),
        encoding="utf-8",
    )
    (source_config / "settings.json").write_text(
        '{"env":{"ANTHROPIC_API_KEY":"wrong-route"}}',
        encoding="utf-8",
    )

    isolated_path: Path | None = None
    with _isolated_claude_config(
        {"CLAUDE_CONFIG_DIR": str(source_config)}
    ) as isolated_config:
        isolated_path = isolated_config
        copied_credentials = isolated_config / ".credentials.json"
        assert sorted(path.name for path in isolated_config.iterdir()) == [
            ".credentials.json"
        ]
        assert json.loads(copied_credentials.read_text(encoding="utf-8")) == {
            "claudeAiOauth": {
                "accessToken": "private-token",
                "refreshToken": "private-refresh",
            }
        }
        assert not (isolated_config / "settings.json").exists()
        if os.name != "nt":
            assert copied_credentials.stat().st_mode & 0o777 == 0o600

    assert isolated_path is not None
    assert not isolated_path.exists()


def test_claude_config_allows_empty_directory_for_macos_keychain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_config = tmp_path / "claude-source"
    source_config.mkdir()
    (source_config / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {"accessToken": "legacy-file-token"},
                "mcpOAuth": {"unrelated-server": {"accessToken": "other"}},
            }
        ),
        encoding="utf-8",
    )
    (source_config / "settings.json").write_text(
        '{"apiKeyHelper":"do-not-load"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "work_buddy.agent_execution.claude_worker._CLAUDE_USES_KEYCHAIN",
        True,
    )

    isolated_path: Path | None = None
    with _isolated_claude_config(
        {"CLAUDE_CONFIG_DIR": str(source_config)}
    ) as isolated_config:
        isolated_path = isolated_config
        assert list(isolated_config.iterdir()) == []

    assert isolated_path is not None
    assert not isolated_path.exists()


def test_claude_config_cleanup_retries_transient_file_locks() -> None:
    calls = 0
    delays: list[float] = []

    def cleanup() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError("credential file is still closing")

    _retry_cleanup(cleanup, sleeper=delays.append)

    assert calls == 3
    assert delays == [0.05, 0.15]


def test_claude_config_cleanup_fails_closed_after_bounded_retries() -> None:
    calls = 0
    delays: list[float] = []

    def cleanup() -> None:
        nonlocal calls
        calls += 1
        raise PermissionError("credential file remains locked")

    with pytest.raises(PermissionError, match="remains locked"):
        _retry_cleanup(cleanup, sleeper=delays.append)

    assert calls == 4
    assert delays == [0.05, 0.15, 0.3]


def test_claude_config_requires_credentials_outside_macos(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_config = tmp_path / "claude-source"
    source_config.mkdir()
    monkeypatch.setattr(
        "work_buddy.agent_execution.claude_worker._CLAUDE_USES_KEYCHAIN",
        False,
    )

    with pytest.raises(FileNotFoundError):
        with _isolated_claude_config(
            {"CLAUDE_CONFIG_DIR": str(source_config)}
        ):
            pytest.fail("Claude must not start without account credentials")


def test_claude_worker_rejects_unsafe_direct_identity() -> None:
    assert (
        run_claude_worker(
            model="sonnet",
            prompt="brief",
            session_id="unsafe\nheader",
            max_budget_usd=1.0,
            command_runner=lambda *_args, **_kwargs: pytest.fail(
                "Claude must not start"
            ),
        )
        == 2
    )


class _FakeCodexProbeClient:
    def __init__(
        self,
        *,
        account_type: str = "chatgpt",
        requires_auth: bool = False,
        fail: Exception | None = None,
    ) -> None:
        self._account_type = account_type
        self._requires_auth = requires_auth
        self._fail = fail

    def __enter__(self) -> "_FakeCodexProbeClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def account(self, *, refresh_token: bool) -> object:
        assert refresh_token is False
        if self._fail is not None:
            raise self._fail
        account = SimpleNamespace(
            type=self._account_type,
            email="private@example.test",
        )
        return SimpleNamespace(
            account=SimpleNamespace(root=account),
            requires_openai_auth=self._requires_auth,
        )

    def models(self, *, include_hidden: bool) -> object:
        assert include_hidden is False
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    model="gpt-visible",
                    display_name="GPT Visible",
                    hidden=False,
                    is_default=True,
                    description="Visible model",
                ),
                SimpleNamespace(
                    model="gpt-hidden",
                    display_name="GPT Hidden",
                    hidden=True,
                    is_default=False,
                    description="Hidden model",
                ),
            ]
        )


def test_codex_sdk_probe_projects_only_chatgpt_and_model_fields() -> None:
    payload = collect_redacted_probe(
        lambda: _FakeCodexProbeClient(requires_auth=True)
    )

    assert payload["availability"] == "ready"
    assert payload["auth_mode"] == "chatgpt"
    assert payload["models"] == [
        {
            "id": "gpt-visible",
            "label": "GPT Visible",
            "available": True,
            "description": "Visible model",
            "unavailable_reason": "",
            "is_default": True,
        }
    ]
    assert "private@example.test" not in json.dumps(payload)


def test_codex_sdk_probe_rejects_api_key_without_leaking_details() -> None:
    payload = collect_redacted_probe(
        lambda: _FakeCodexProbeClient(account_type="apiKey")
    )
    failed = collect_redacted_probe(
        lambda: _FakeCodexProbeClient(
            fail=RuntimeError("secret-token-and-private-path")
        )
    )

    assert payload["availability"] == "unavailable"
    assert payload["auth_mode"] == "chatgpt"
    assert "API" not in payload["unavailable_reason"]
    assert "secret-token-and-private-path" not in json.dumps(failed)
    assert failed["availability"] == "unknown"


def test_codex_provider_parses_redacted_probe_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "not-in-probe")
    monkeypatch.setenv("CODEX_API_KEY", "also-not-in-probe")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("CODEX_MODEL_PROVIDER", "custom-provider")
    calls: list[list[str]] = []
    payload = {
        "availability": "ready",
        "auth_mode": "chatgpt",
        "models": [
            {
                "id": "gpt-5.6-sol",
                "label": "GPT-5.6 Sol",
                "available": True,
                "description": "Frontier",
                "is_default": True,
            }
        ],
        "unavailable_reason": "",
        "state_key": "safe-key",
    }

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["shell"] is False
        child_env = kwargs["env"]
        assert isinstance(child_env, dict)
        assert "OPENAI_API_KEY" not in child_env
        assert "CODEX_API_KEY" not in child_env
        assert "OPENAI_BASE_URL" not in child_env
        assert "CODEX_MODEL_PROVIDER" not in child_env
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="account@example.test",
        )

    provider = CodexProvider(command_runner=run)
    descriptor = provider.probe()
    assert provider.probe() is descriptor

    assert calls == [
        [
            sys.executable,
            "-m",
            "work_buddy.agent_execution.codex_probe_worker",
        ]
    ]
    assert descriptor.available
    assert descriptor.state_key == "safe-key"
    validated = provider.validate_selection(
        AgentExecutionSelection("codex", "gpt-5.6-sol")
    )
    assert validated.to_dict() == {
        "provider_id": "codex",
        "model_id": "gpt-5.6-sol",
        "provider_label": "Codex",
        "model_label": "GPT-5.6 Sol",
    }
    assert "account@example.test" not in json.dumps(descriptor.to_dict())


def test_codex_start_uses_worker_stdin_exact_model_and_no_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "not-in-child")
    monkeypatch.setenv("CODEX_API_KEY", "also-not-in-child")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("CODEX_MODEL_PROVIDER", "custom-provider")
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "inherited-bootstrap-id")
    probe_payload = {
        "availability": "ready",
        "models": [
            {
                "id": "gpt-selected",
                "label": "GPT Selected",
                "available": True,
            }
        ],
    }

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(probe_payload),
            stderr="",
        )

    captured: dict[str, object] = {}

    def spawn(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "ok", "pid": 84}

    monkeypatch.setattr(
        "work_buddy.sidecar.dispatch.executor.spawn_detached_process_authorized",
        spawn,
    )
    provider = CodexProvider(command_runner=run)
    outcome = provider.start_detached(
        AgentSpawnRequest(
            name="document",
            prompt="private brief",
            selection=AgentExecutionSelection("codex", "gpt-selected"),
            working_directory=tmp_path,
            session_id="work-buddy-session",
        )
    )

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[:3] == [
        sys.executable,
        "-m",
        "work_buddy.agent_execution.codex_worker",
    ]
    assert argv[argv.index("--model") + 1] == "gpt-selected"
    assert captured["session_name"] == "work-buddy-session"
    assert "private brief" not in argv
    assert str(tmp_path.resolve()) not in argv
    stdin_text = captured["stdin_text"]
    assert isinstance(stdin_text, str)
    assert '`session_id="work-buddy-session"`' in stdin_text
    assert '`harness_id="codexcli"`' in stdin_text
    assert "`workspace=" not in stdin_text
    assert str(tmp_path.resolve()) not in stdin_text
    assert "inherited-bootstrap-id" not in stdin_text
    assert stdin_text.endswith("private brief")
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "OPENAI_API_KEY" not in child_env
    assert "CODEX_API_KEY" not in child_env
    assert "OPENAI_BASE_URL" not in child_env
    assert "CODEX_MODEL_PROVIDER" not in child_env
    assert child_env["WORK_BUDDY_SESSION_ID"] == "work-buddy-session"
    assert outcome.ok and outcome.session_id == "work-buddy-session"


def test_codex_worker_uses_read_only_ephemeral_exact_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WORK_BUDDY_SESSION_ID", "before-worker-test")
    calls: dict[str, object] = {}
    instances: list[object] = []

    discovered_config = _isolated_effective_codex_config(
        "node_repl",
        "work-buddy",
        plugin_ids=("personal@example",),
    )
    isolated_config = _isolated_effective_codex_config(
        "node_repl",
        "work-buddy",
        plugin_ids=("personal@example",),
    )
    for entry in isolated_config["mcp_servers"].values():  # type: ignore[union-attr]
        entry["enabled"] = False
    for entry in isolated_config["plugins"].values():  # type: ignore[union-attr]
        entry["enabled"] = False

    class FakeThread:
        def run(self, prompt: str, **kwargs: object) -> object:
            calls["run"] = (prompt, kwargs)
            return SimpleNamespace(final_response="done")

    class FakeCodex:
        def __init__(self) -> None:
            instance_index = len(instances)
            instances.append(self)
            effective_config = (
                discovered_config
                if instance_index == 0
                else isolated_config
            )
            self._client = SimpleNamespace(
                _request_raw=lambda method, params: {
                    "config": effective_config
                }
            )

        def __enter__(self) -> "FakeCodex":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def thread_start(self, **kwargs: object) -> FakeThread:
            calls["thread_start"] = kwargs
            return FakeThread()

    result = run_worker(
        model="gpt-exact",
        prompt="agent brief",
        session_id="generation-cowork",
        mcp_url="http://localhost:5126/mcp",
        codex_factory=FakeCodex,
    )

    assert result == 0
    assert len(instances) == 2
    assert calls["thread_start"]["model"] == "gpt-exact"  # type: ignore[index]
    assert calls["thread_start"]["model_provider"] == "openai"  # type: ignore[index]
    assert calls["thread_start"]["ephemeral"] is True  # type: ignore[index]
    assert calls["thread_start"]["sandbox"].value == "read-only"  # type: ignore[index]
    assert calls["run"][0] == "agent brief"  # type: ignore[index]
    assert calls["run"][1]["model"] == "gpt-exact"  # type: ignore[index]
    assert calls["run"][1]["sandbox"].value == "read-only"  # type: ignore[index]
    assert calls["thread_start"]["config"] == build_thread_config(  # type: ignore[index]
        mcp_url="http://localhost:5126/mcp",
        session_id="generation-cowork",
        inherited_mcp_server_names=("node_repl", "work-buddy"),
    )
    assert calls["thread_start"]["config"]["mcp_servers"].keys() == {  # type: ignore[index]
        "node_repl",
        "work-buddy",
    }
    assert (
        calls["thread_start"]["config"]["mcp_servers"]["work-buddy"][  # type: ignore[index]
            "default_tools_approval_mode"
        ]
        == "approve"
    )
    assert (
        calls["thread_start"]["config"]["mcp_servers"]["node_repl"][  # type: ignore[index]
            "enabled"
        ]
        is False
    )
    assert (
        calls["thread_start"]["config"]["mcp_servers"]["work-buddy"][  # type: ignore[index]
            "http_headers"
        ]
        == {"X-Work-Buddy-Session": "generation-cowork"}
    )
    thread_cwd = Path(calls["thread_start"]["cwd"])  # type: ignore[index]
    assert thread_cwd != tmp_path.resolve()
    assert not thread_cwd.exists()
    assert str(tmp_path.resolve()) not in calls["thread_start"][  # type: ignore[operator]
        "base_instructions"
    ]
    assert "Work Buddy MCP server" in calls["thread_start"][  # type: ignore[operator]
        "base_instructions"
    ]
    assert "not as permission" in calls["thread_start"][  # type: ignore[operator]
        "developer_instructions"
    ]


def test_codex_worker_rejects_nonlocal_mcp_endpoint() -> None:
    with pytest.raises(ValueError):
        _local_mcp_url("https://example.com/mcp")
    with pytest.raises(ValueError):
        _local_mcp_url("http://localhost:5126/not-mcp")
    with pytest.raises(ValueError):
        _local_mcp_url("http://localhost:5126/mcp?redirect=elsewhere")


class _FakeProvider:
    def __init__(self, provider_id: str, label: str, model_id: str) -> None:
        self.provider_id = provider_id
        self.label = label
        self.auth_mode = "test"
        self.default_model_id = model_id
        self.model_id = model_id
        self.probe_count = 0
        self.start_requests: list[AgentSpawnRequest] = []

    def probe(self, *, refresh: bool = False) -> ProviderDescriptor:
        self.probe_count += 1
        return ProviderDescriptor(
            id=self.provider_id,
            label=self.label,
            availability=ProviderAvailability.READY,
            auth_mode=self.auth_mode,
            models=(
                ModelDescriptor(
                    id=self.model_id,
                    label=f"{self.label} Model",
                    is_default=True,
                ),
            ),
        )

    def validate_selection(
        self,
        selection: AgentExecutionSelection,
        *,
        refresh: bool = False,
    ) -> AgentExecutionSelection:
        self.probe(refresh=refresh)
        if selection.model_id != self.model_id:
            raise UnknownModelError(selection.model_id)
        return AgentExecutionSelection(
            provider_id=self.provider_id,
            model_id=self.model_id,
            provider_label=self.label,
            model_label=f"{self.label} Model",
        )

    def start_detached(self, request: AgentSpawnRequest) -> object:
        self.start_requests.append(request)
        return SimpleNamespace(selection=request.selection)


def test_registry_default_is_deterministic_without_probe_and_dispatches_validated(
    tmp_path: Path,
) -> None:
    provider = _FakeProvider("test", "Test", "model")
    default = AgentExecutionSelection(
        "test",
        "model",
        "Test",
        "Test Model",
    )
    registry = ProviderRegistry([provider], default_selection=default)

    assert registry.default_selection is default
    assert provider.probe_count == 0
    assert registry.get_catalog().default_selection is default
    assert provider.probe_count == 1
    outcome = registry.start_detached(
        AgentSpawnRequest(
            name="document",
            prompt="brief",
            selection=AgentExecutionSelection("test", "model"),
            session_id="registry-session",
            working_directory=tmp_path,
        )
    )
    assert outcome.selection.provider_label == "Test"
    assert provider.start_requests[0].selection.model_label == "Test Model"
    with pytest.raises(UnknownProviderError):
        registry.validate_selection(
            AgentExecutionSelection("missing", "model")
        )


def test_global_registry_preserves_configured_supported_claude_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from work_buddy.settings import store as settings_store

    monkeypatch.setattr(settings_store, "_db_path", lambda: tmp_path / "settings.db")
    monkeypatch.setattr(
        execution_registry,
        "load_config",
        lambda: {
            "sidecar": {
                "agent_spawn": {
                    "model": "opus",
                }
            }
        },
    )
    monkeypatch.setattr(execution_registry, "_registry", None)

    selected = execution_registry.default_selection()

    assert selected == AgentExecutionSelection(
        provider_id="claude-code",
        model_id="opus",
        provider_label="Claude Code",
        model_label="Opus",
    )


def test_spawn_request_requires_safe_caller_supplied_session_id() -> None:
    with pytest.raises(ValueError, match="session_id"):
        AgentSpawnRequest(
            name="document",
            prompt="brief",
            selection=AgentExecutionSelection("claude-code", "sonnet"),
            session_id="",
        )
    with pytest.raises(ValueError, match="session_id"):
        AgentSpawnRequest(
            name="document",
            prompt="brief",
            selection=AgentExecutionSelection("claude-code", "sonnet"),
            session_id="unsafe\ninjection",
        )


def test_registry_catalog_probes_concurrently_preserves_order_and_isolates_failure(
) -> None:
    barrier = threading.Barrier(2)

    class ConcurrentProvider(_FakeProvider):
        def probe(self, *, refresh: bool = False) -> ProviderDescriptor:
            barrier.wait(timeout=2)
            return super().probe(refresh=refresh)

    class FailingProvider(_FakeProvider):
        def probe(self, *, refresh: bool = False) -> ProviderDescriptor:
            barrier.wait(timeout=2)
            raise RuntimeError("private-account-and-runtime-detail")

    first = ConcurrentProvider("first", "First", "model-1")
    second = FailingProvider("second", "Second", "model-2")
    registry = ProviderRegistry(
        [first, second],
        default_selection=AgentExecutionSelection(
            "first",
            "model-1",
            "First",
            "First Model",
        ),
    )

    catalog = registry.get_catalog(refresh=True)

    assert [provider.id for provider in catalog.providers] == [
        "first",
        "second",
    ]
    assert catalog.providers[0].availability is ProviderAvailability.READY
    assert catalog.providers[1].availability is ProviderAvailability.UNKNOWN
    assert catalog.providers[1].unavailable_reason == (
        "Second couldn't be checked."
    )
    assert "private-account" not in json.dumps(catalog.to_dict())


def test_codex_subscription_config_forces_minimal_chatgpt_host(
    tmp_path: Path,
) -> None:
    config = codex_subscription_config(
        cwd=tmp_path,
        env={
            "OPENAI_API_KEY": "secret",
            "CODEX_API_KEY": "secret",
            "KEEP": "yes",
        },
    )

    assert config.cwd == str(tmp_path.resolve())
    assert config.env == {"KEEP": "yes"}
    overrides = set(config.config_overrides)
    assert {
        'forced_login_method="chatgpt"',
        'model_provider="openai"',
        'chatgpt_base_url="https://chatgpt.com/backend-api/"',
        'openai_base_url="https://api.openai.com/v1"',
        'approval_policy="never"',
        'sandbox_mode="read-only"',
        "allow_login_shell=false",
        "features.hooks=false",
        "features.apps=false",
        "features.plugins=false",
        "features.plugin_sharing=false",
        "features.multi_agent=false",
        "features.shell_tool=false",
        "features.shell_snapshot=false",
        "features.unified_exec=false",
        "features.network_proxy=false",
        "features.browser_use=false",
        "features.computer_use=false",
        "features.code_mode=false",
        "features.workspace_dependencies=false",
        "features.tool_suggest=false",
        'web_search="disabled"',
        "tools.web_search=false",
        "tools.view_image=false",
        "apps._default.enabled=false",
        "developer_instructions=\"\"",
        "instructions=\"\"",
        "compact_prompt=\"\"",
        "project_doc_max_bytes=0",
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
    } <= overrides
    assert not any(
        override.startswith(("mcp_servers.", "plugins."))
        for override in overrides
    )


def test_codex_config_read_enumerates_inherited_mcps_and_fails_closed(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    effective = _isolated_effective_codex_config(
        "z-user-server",
        "a-project-server",
    )

    def request_raw(
        method: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        calls.append((method, params))
        return {"config": effective}

    codex = SimpleNamespace(
        _client=SimpleNamespace(_request_raw=request_raw)
    )
    config = read_effective_codex_config(codex, cwd=tmp_path)
    validate_subscription_codex_config(config)

    assert effective_mcp_server_names(config) == (
        "a-project-server",
        "z-user-server",
    )
    assert calls == [
        (
            "config/read",
            {
                "cwd": str(tmp_path.resolve()),
                "includeLayers": False,
            },
        )
    ]
    with pytest.raises(CodexConfigIsolationError):
        read_effective_codex_config(SimpleNamespace(), cwd=tmp_path)
    with pytest.raises(CodexConfigIsolationError):
        validate_subscription_codex_config(
            {
                **effective,
                "model_provider": "custom",
            }
        )
    with pytest.raises(CodexConfigIsolationError):
        validate_subscription_codex_config(
            {
                **effective,
                "model_instructions_file": "private-instructions.md",
            }
        )
    with pytest.raises(CodexConfigIsolationError):
        validate_subscription_codex_config(
            {
                **effective,
                "model_catalog_json": "private-models.json",
            }
        )
    with pytest.raises(CodexConfigIsolationError):
        effective_mcp_server_names(
            {
                **effective,
                "mcp_servers": ["not", "a", "mapping"],
            }
        )


def test_codex_entry_isolation_replaces_transports_and_verifies_every_entry(
) -> None:
    discovered = _isolated_effective_codex_config(
        "command.server",
        plugin_ids=("plugin@example",),
    )
    discovered["mcp_servers"]["url/server"] = {  # type: ignore[index]
        "url": "https://untrusted.example.test/mcp",
        "enabled": True,
    }

    overrides = codex_entry_isolation_overrides(
        effective_config=discovered,
        plugin_ids=effective_plugin_ids(discovered),
    )

    assert len(overrides) == 2
    assert overrides[0].startswith("mcp_servers={")
    assert '"command.server"=' in overrides[0]
    assert f"command={json.dumps(sys.executable)}" in overrides[0]
    assert 'args=["-c","pass"]' in overrides[0]
    assert '"url/server"=' in overrides[0]
    assert 'url="http://127.0.0.1:9/work-buddy-disabled"' in overrides[0]
    assert overrides[0].count("enabled=false") == 2
    assert overrides[1] == 'plugins={"plugin@example"={enabled=false}}'

    isolated = _isolated_effective_codex_config(
        "command.server",
        plugin_ids=("plugin@example",),
    )
    isolated["mcp_servers"]["url/server"] = {  # type: ignore[index]
        "url": "http://127.0.0.1:9/work-buddy-disabled",
        "enabled": False,
    }
    isolated["mcp_servers"]["command.server"]["enabled"] = False  # type: ignore[index]
    isolated["plugins"]["plugin@example"]["enabled"] = False  # type: ignore[index]
    validate_entry_isolation(
        isolated,
        mcp_server_names=("command.server", "url/server"),
        plugin_ids=("plugin@example",),
    )

    isolated["plugins"]["late-plugin"] = {"enabled": False}  # type: ignore[index]
    with pytest.raises(CodexConfigIsolationError):
        validate_entry_isolation(
            isolated,
            mcp_server_names=("command.server", "url/server"),
            plugin_ids=("plugin@example",),
        )


def test_codex_worker_fails_closed_without_config_read(
    tmp_path: Path,
) -> None:
    thread_started = False

    class FakeCodex:
        def __enter__(self) -> "FakeCodex":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def thread_start(self, **_kwargs: object) -> object:
            nonlocal thread_started
            thread_started = True
            raise AssertionError("thread must not start")

    assert (
        run_worker(
            model="gpt-exact",
            prompt="agent brief",
            session_id="generated-session",
            mcp_url="http://localhost:5126/mcp",
            codex_factory=FakeCodex,
        )
        == 1
    )
    assert thread_started is False


def test_codex_worker_rejects_unsafe_direct_session_identity(
    tmp_path: Path,
) -> None:
    assert (
        run_worker(
            model="gpt-exact",
            prompt="agent brief",
            session_id="unsafe\nheader",
            mcp_url="http://localhost:5126/mcp",
            codex_factory=lambda: object(),
        )
        == 2
    )


def test_environment_sanitizers_are_case_insensitive() -> None:
    assert claude_account_environment(
        {
            "Anthropic_Api_Key": "secret",
            "ANTHROPIC_MODEL": "claude-rerouted",
            "aNtHrOpIc_Future_Credential": "future-secret",
            "SUBAGENT_ANTHROPIC_API_KEY": "secret",
            "SubAgent_Anthropic_Future_Route": "future-route",
            "ANTHROPIC_AUTH_TOKEN": "secret",
            "ANTHROPIC_BASE_URL": "https://gateway.example.test",
            "Claude_Code_OAuth_Token": "different-account",
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "refresh",
            "claude_code_oauth_scopes": "user:profile",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "CLAUDE_CODE_USE_MANTLE": "1",
            "CLAUDE_CODE_USE_GATEWAY": "1",
            "CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS": "1",
            "ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock.example.test",
            "ANTHROPIC_VERTEX_BASE_URL": "https://vertex.example.test",
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://foundry.example.test",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "500",
            "Claude_Code_Enable_Telemetry": "1",
            "cLaUdE_CoDe_EnHaNcEd_TeLeMeTrY_BeTa": "1",
            "Enable_Enhanced_Telemetry_Beta": "1",
            "oTeL_LoGs_ExPoRtEr": "otlp",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example.test",
            "otel_log_raw_api_bodies": "1",
            "OtEl_LoG_UsEr_PrOmPtS": "1",
            "TraceParent": "00-secret-parent",
            "TRACESTATE": "vendor=secret",
            "Claude_Code_Disable_Nonessential_Traffic": "0",
            "Disable_Telemetry": "0",
            "DISABLE_ERROR_REPORTING": "0",
            "Do_Not_Track": "0",
            "Claude_Code_Auto_Connect_Ide": "true",
            "Claude_Code_Disable_Attachments": "0",
            "Claude_Code_Disable_Auto_Memory": "0",
            "Claude_Code_Disable_Claude_Mds": "0",
            "claude_code_ide_skip_auto_install": "0",
            "DEBUG": "claude:*",
            "Claude_Debug": "1",
            "DEBUG_CLAUDE_AGENT_SDK": "1",
            "DEBUG_SDK": "1",
            "Claude_Code_Debug_Logs_Dir": "C:/private/debug",
            "CLAUDE_CODE_SYNC_PLUGINS": "1",
            "CLAUDE_CODE_SYNC_PLUGIN_INSTALL": "1",
            "CLAUDE_CODE_SYNC_SKILLS": "1",
            "CLAUDE_CODE_ENABLE_BACKGROUND_PLUGIN_REFRESH": "1",
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "0",
            "FORCE_AUTOUPDATE_PLUGINS": "1",
            "CLAUDE_CODE_MANAGED_SETTINGS_PATH": "C:/untrusted/policy.json",
            "HTTPS_PROXY": "https://proxy.example.test",
            "SSL_CERT_FILE": "certificate.pem",
            "KEEP": "yes",
        }
    ) == {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "0",
        "ENABLE_ENHANCED_TELEMETRY_BETA": "0",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_AUTO_CONNECT_IDE": "false",
        "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL": "1",
        "CLAUDE_CODE_SYNC_PLUGINS": "0",
        "CLAUDE_CODE_SYNC_PLUGIN_INSTALL": "0",
        "CLAUDE_CODE_SYNC_SKILLS": "0",
        "CLAUDE_CODE_ENABLE_BACKGROUND_PLUGIN_REFRESH": "0",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
        "HTTPS_PROXY": "https://proxy.example.test",
        "SSL_CERT_FILE": "certificate.pem",
        "KEEP": "yes",
    }
    assert codex_chatgpt_environment(
        {
            "OpenAI_Api_Key": "secret",
            "AZURE_OPENAI_API_KEY": "secret",
            "CODEX_API_KEY": "not-managed-session-plumbing",
            "OPENAI_BASE_URL": "https://api.example.test",
            "CODEX_BASE_URL": "https://codex.example.test",
            "OPENAI_MODEL_PROVIDER": "custom",
            "CODEX_MODEL_PROVIDER": "custom",
            "CODEX_HOME": "C:/safe/auth-location",
            "HTTPS_PROXY": "https://proxy.example.test",
            "KEEP": "yes",
        }
    ) == {
        "CODEX_HOME": "C:/safe/auth-location",
        "HTTPS_PROXY": "https://proxy.example.test",
        "KEEP": "yes",
    }
