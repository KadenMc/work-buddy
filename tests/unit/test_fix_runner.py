"""Tests for the requirement-fix dispatcher (Fix-A)."""

from __future__ import annotations

from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _reset_graph_cache():
    from work_buddy.control.graph import invalidate_graph
    invalidate_graph()
    yield
    invalidate_graph()


# ---------------------------------------------------------------------------
# Dispatcher: schema validation + dispatch
# ---------------------------------------------------------------------------

def test_run_fix_unknown_requirement_returns_ok_false():
    from work_buddy.control.fix_runner import run_fix
    result = run_fix("does/not/exist")
    assert result["ok"] is False
    assert "Unknown requirement" in result["detail"]


def test_run_fix_no_fix_returns_ok_false():
    """A requirement with fix_kind='none' refuses politely. Uses a
    synthesized req so the test is decoupled from which real
    requirements happen to be wired up at any given moment."""
    from work_buddy.control.fix_runner import run_fix
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    fake = mock.Mock()
    fake.id = "fake/no-fix"
    fake.fix_kind = "none"
    fake.fix_hint = "do it manually"
    with mock.patch.dict(REQUIREMENT_REGISTRY, {"fake/no-fix": fake}, clear=False):
        result = run_fix("fake/no-fix")
    assert result["ok"] is False
    assert "no automated fix" in result["detail"].lower()


def test_run_fix_input_required_missing_params():
    """input_required requirements reject calls with missing fields."""
    from work_buddy.control.fix_runner import run_fix
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    # Synthesize an input_required req for the duration of this test
    fake = mock.Mock()
    fake.fix_kind = "input_required"
    fake.fix_fn = "tests.unit.test_fix_runner._noop_fixer"
    fake.fix_params = {"path": {"required": True}}
    with mock.patch.dict(REQUIREMENT_REGISTRY, {"fake/req": fake}, clear=False):
        result = run_fix("fake/req", params={})
    assert result["ok"] is False
    assert "Missing required input" in result["detail"]


def _noop_fixer(**kwargs):
    return {"ok": True, "detail": "ok", "side_effects": []}


# ---------------------------------------------------------------------------
# Smoke fix: data/ writable
# ---------------------------------------------------------------------------

def test_fix_data_writable_creates_missing_dir(tmp_path, monkeypatch):
    """fix_data_writable creates the data dir if missing and reports the path."""
    target = tmp_path / "data"
    assert not target.exists()
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda *a, **k: target)
    from work_buddy.health.fixers import fix_data_writable
    result = fix_data_writable()
    assert result["ok"] is True
    assert target.exists() and target.is_dir()
    assert any("Created" in s for s in result["side_effects"])


def test_fix_data_writable_idempotent(tmp_path, monkeypatch):
    """Second invocation when dir already exists is a no-op success."""
    target = tmp_path / "data"
    target.mkdir()
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda *a, **k: target)
    from work_buddy.health.fixers import fix_data_writable
    result = fix_data_writable()
    assert result["ok"] is True
    assert result["side_effects"] == []  # nothing new to do


# ---------------------------------------------------------------------------
# End-to-end via run_fix: smoke fix actually applies
# ---------------------------------------------------------------------------

def test_run_fix_smoke_end_to_end(tmp_path, monkeypatch):
    """run_fix('core/data/writable') dispatches to the smoke fixer and
    re-runs the check, returning the fresh recheck result."""
    target = tmp_path / "data"
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda *a, **k: target)
    from work_buddy.control.fix_runner import run_fix
    result = run_fix("core/data/writable")
    assert result["ok"] is True
    assert result["recheck"] is not None
    assert result["recheck"]["ok"] is True
    assert target.exists()


# ---------------------------------------------------------------------------
# Fix-B: directory creators
# ---------------------------------------------------------------------------

def test_fix_journal_dir_creates(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": str(vault), "obsidian": {"journal_dir": "journal"}},
    )
    from work_buddy.health.fixers import fix_journal_dir
    result = fix_journal_dir()
    assert result["ok"] is True
    assert (vault / "journal").is_dir()
    assert any("Created" in s for s in result["side_effects"])


def test_fix_journal_dir_no_vault_root(monkeypatch):
    monkeypatch.setattr("work_buddy.config.load_config", lambda: {"vault_root": ""})
    from work_buddy.health.fixers import fix_journal_dir
    result = fix_journal_dir()
    assert result["ok"] is False
    assert "vault_root" in result["detail"].lower()


def test_fix_contracts_dir(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "vault_root": str(vault),
            "contracts": {"vault_path": "wb/contracts"},
        },
    )
    from work_buddy.health.fixers import fix_contracts_dir
    result = fix_contracts_dir()
    assert result["ok"] is True
    assert (vault / "wb" / "contracts").is_dir()


def test_fix_personal_knowledge_dir(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {
            "vault_root": str(vault),
            "personal_knowledge": {"vault_path": "Meta/PK"},
        },
    )
    from work_buddy.health.fixers import fix_personal_knowledge_dir
    result = fix_personal_knowledge_dir()
    assert result["ok"] is True
    assert (vault / "Meta" / "PK").is_dir()


def test_fix_master_task_list_creates_with_seed(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": str(vault)},
    )
    from work_buddy.health.fixers import fix_master_task_list
    result = fix_master_task_list()
    assert result["ok"] is True
    target = vault / "tasks" / "master-task-list.md"
    assert target.exists()
    assert "Master Task List" in target.read_text(encoding="utf-8")


def test_fix_master_task_list_idempotent(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "tasks").mkdir(parents=True)
    existing = vault / "tasks" / "master-task-list.md"
    existing.write_text("user content", encoding="utf-8")
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": str(vault)},
    )
    from work_buddy.health.fixers import fix_master_task_list
    result = fix_master_task_list()
    assert result["ok"] is True
    # Existing content preserved — fix is idempotent, doesn't overwrite
    assert existing.read_text(encoding="utf-8") == "user content"


# ---------------------------------------------------------------------------
# Fix-B: daily-note section appenders
# ---------------------------------------------------------------------------

def test_fix_log_section_creates_today_when_no_note_exists(tmp_path, monkeypatch):
    """If no daily note exists at all, fix creates today's note with the section."""
    vault = tmp_path / "vault"
    journal = vault / "journal"
    journal.mkdir(parents=True)
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": str(vault), "obsidian": {"journal_dir": "journal"}},
    )
    from datetime import date
    from work_buddy.health.fixers import fix_log_section
    result = fix_log_section()
    assert result["ok"] is True
    today_path = journal / f"{date.today().strftime('%Y-%m-%d')}.md"
    assert today_path.exists()
    assert "# Log" in today_path.read_text(encoding="utf-8")


def test_fix_log_section_appends_to_yesterday_if_today_missing(tmp_path, monkeypatch):
    """If yesterday's note exists but today's doesn't, fix appends to yesterday."""
    from datetime import date, timedelta
    vault = tmp_path / "vault"
    journal = vault / "journal"
    journal.mkdir(parents=True)
    y = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday = journal / f"{y}.md"
    yesterday.write_text("# Existing\n\nstuff\n", encoding="utf-8")
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": str(vault), "obsidian": {"journal_dir": "journal"}},
    )
    from work_buddy.health.fixers import fix_log_section
    result = fix_log_section()
    assert result["ok"] is True
    content = yesterday.read_text(encoding="utf-8")
    assert "# Log" in content
    assert "# Existing" in content  # preserved
    assert y in result["detail"]


def test_fix_log_section_idempotent_when_section_present(tmp_path, monkeypatch):
    from datetime import date
    vault = tmp_path / "vault"
    journal = vault / "journal"
    journal.mkdir(parents=True)
    today = journal / f"{date.today().strftime('%Y-%m-%d')}.md"
    today.write_text("# Log\n\n", encoding="utf-8")
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": str(vault), "obsidian": {"journal_dir": "journal"}},
    )
    from work_buddy.health.fixers import fix_log_section
    result = fix_log_section()
    assert result["ok"] is True
    assert "already present" in result["detail"]
    assert result["side_effects"] == []


def test_fix_sign_in_and_running_notes(tmp_path, monkeypatch):
    """The other two section fixers wrap the same primitive."""
    vault = tmp_path / "vault"
    journal = vault / "journal"
    journal.mkdir(parents=True)
    monkeypatch.setattr(
        "work_buddy.config.load_config",
        lambda: {"vault_root": str(vault), "obsidian": {"journal_dir": "journal"}},
    )
    from work_buddy.health.fixers import fix_sign_in_section, fix_running_notes_section
    r1 = fix_sign_in_section()
    r2 = fix_running_notes_section()
    assert r1["ok"] and r2["ok"]
    from datetime import date
    content = (journal / f"{date.today().strftime('%Y-%m-%d')}.md").read_text(encoding="utf-8")
    assert "# Sign-In" in content
    assert "# Running Notes" in content


# ---------------------------------------------------------------------------
# Fix-C: input_required fixers
# ---------------------------------------------------------------------------

def test_fix_repos_root_rejects_nonexistent_path(tmp_path):
    from work_buddy.health.fixers import fix_repos_root
    result = fix_repos_root(path=str(tmp_path / "nope"))
    assert result["ok"] is False
    assert "not an existing directory" in result["detail"]


def test_fix_repos_root_writes_config(tmp_path, monkeypatch):
    """Validate the path, then write to config.yaml."""
    repos = tmp_path / "code"
    repos.mkdir()
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("repos_root: \"\"\nvault_root: \"/old\"\n", encoding="utf-8")
    monkeypatch.setattr(
        "work_buddy.health.fixers._repo_root",
        lambda: tmp_path,
    )
    from work_buddy.health.fixers import fix_repos_root
    result = fix_repos_root(path=str(repos))
    assert result["ok"] is True
    written = config_yaml.read_text(encoding="utf-8")
    assert str(repos).replace("\\", "\\\\") in written.replace("\\", "\\\\") or str(repos) in written


def test_fix_timezone_rejects_invalid():
    from work_buddy.health.fixers import fix_timezone
    result = fix_timezone(timezone="Not/A/Zone")
    assert result["ok"] is False
    assert "valid IANA" in result["detail"]


def test_fix_timezone_writes_config(tmp_path, monkeypatch):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("timezone: \"\"\n", encoding="utf-8")
    monkeypatch.setattr(
        "work_buddy.health.fixers._repo_root",
        lambda: tmp_path,
    )
    from work_buddy.health.fixers import fix_timezone
    result = fix_timezone(timezone="America/Toronto")
    assert result["ok"] is True
    assert "America/Toronto" in config_yaml.read_text(encoding="utf-8")


def test_fix_anthropic_api_key_validates_prefix():
    from work_buddy.health.fixers import fix_anthropic_api_key
    bad = fix_anthropic_api_key(api_key="not-a-key")
    assert bad["ok"] is False
    assert "sk-" in bad["detail"]


def test_fix_anthropic_api_key_creates_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "work_buddy.health.fixers._repo_root",
        lambda: tmp_path,
    )
    monkeypatch.delenv("SUBAGENT_ANTHROPIC_API_KEY", raising=False)
    from work_buddy.health.fixers import fix_anthropic_api_key
    result = fix_anthropic_api_key(api_key="sk-ant-test-12345")
    assert result["ok"] is True
    env_file = tmp_path / ".env"
    assert env_file.exists()
    content = env_file.read_text(encoding="utf-8")
    assert "SUBAGENT_ANTHROPIC_API_KEY=sk-ant-test-12345" in content
    # In-memory env updated too — recheck after the fix sees the value
    import os
    assert os.environ["SUBAGENT_ANTHROPIC_API_KEY"] == "sk-ant-test-12345"


def test_fix_anthropic_api_key_replaces_existing_line(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OTHER=stays\n"
        "SUBAGENT_ANTHROPIC_API_KEY=sk-old-old-old\n"
        "ALSO=stays\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "work_buddy.health.fixers._repo_root",
        lambda: tmp_path,
    )
    from work_buddy.health.fixers import fix_anthropic_api_key
    result = fix_anthropic_api_key(api_key="sk-ant-new-key")
    assert result["ok"] is True
    new_content = env_file.read_text(encoding="utf-8")
    assert "OTHER=stays" in new_content
    assert "ALSO=stays" in new_content
    assert "sk-old-old-old" not in new_content
    assert "SUBAGENT_ANTHROPIC_API_KEY=sk-ant-new-key" in new_content


def test_fix_telegram_bot_token_validates_shape():
    from work_buddy.health.fixers import fix_telegram_bot_token
    bad = fix_telegram_bot_token(bot_token="just-a-string")
    assert bad["ok"] is False
    assert "Telegram bot token" in bad["detail"]


def test_fix_telegram_bot_token_writes_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "work_buddy.health.fixers._repo_root",
        lambda: tmp_path,
    )
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    from work_buddy.health.fixers import fix_telegram_bot_token
    result = fix_telegram_bot_token(bot_token="123456789:AABBCCDDEEFFGG-test-token-12345")
    assert result["ok"] is True
    assert "TELEGRAM_BOT_TOKEN" in (tmp_path / ".env").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fix-D: agent_handoff dispatch
# ---------------------------------------------------------------------------

def test_run_fix_agent_handoff_spawns_session():
    """An agent_handoff requirement triggers a Claude Code spawn with
    the registered fix_agent_brief as the prompt. Verified by mocking
    begin_session."""
    from work_buddy.control.fix_runner import run_fix
    with mock.patch(
        "work_buddy.session_launcher.begin_session",
        return_value={"status": "ok", "session_id": "s-test", "pid": 42, "message": "ok"},
    ) as mock_begin, \
         mock.patch("work_buddy.consent.grant_consent"):
        # obsidian/plugins/tasks-plugin is registered as agent_handoff
        result = run_fix("obsidian/plugins/tasks-plugin")
    assert result["ok"] is True
    assert result["spawned"] is not None
    assert result["spawned"]["pid"] == 42
    assert mock_begin.called
    # The spawned prompt should be the registered fix_agent_brief
    sent_prompt = mock_begin.call_args.kwargs.get("prompt") or mock_begin.call_args.args[0]
    assert "Tasks community plugin" in sent_prompt or "obsidian-tasks-plugin" in sent_prompt


def test_run_fix_agent_handoff_falls_back_to_generic_brief():
    """When fix_agent_brief is None, a generic brief is built from
    the requirement metadata."""
    from work_buddy.control.fix_runner import run_fix
    from work_buddy.health.requirements import REQUIREMENT_REGISTRY

    fake = mock.Mock()
    fake.id = "fake/handoff"
    fake.fix_kind = "agent_handoff"
    fake.fix_agent_brief = None
    fake.description = "Test fake handoff"
    fake.severity = "required"
    fake.fix_hint = "Manual steps go here"
    with mock.patch.dict(REQUIREMENT_REGISTRY, {"fake/handoff": fake}, clear=False), \
         mock.patch(
             "work_buddy.session_launcher.begin_session",
             return_value={"status": "ok", "session_id": "s", "pid": 1, "message": "ok"},
         ) as mock_begin, \
         mock.patch("work_buddy.consent.grant_consent"):
        result = run_fix("fake/handoff")
    assert result["ok"] is True
    sent = mock_begin.call_args.kwargs.get("prompt") or mock_begin.call_args.args[0]
    assert "fake/handoff" in sent
    assert "Manual steps go here" in sent


# ---------------------------------------------------------------------------
# Help-brief builder (Fix-A: replaces Status-tab diagnose hint)
# ---------------------------------------------------------------------------

def test_help_brief_for_unknown_node():
    from work_buddy.control.help_briefs import build_help_brief
    brief = build_help_brief("not:a:real:node")
    assert "not currently in the control graph" in brief


def test_help_brief_for_requirement_includes_metadata():
    from work_buddy.control.help_briefs import build_help_brief
    # Pick a real registered requirement
    brief = build_help_brief("req:core/data/writable")
    assert "core/data/writable" in brief
    assert "data/" in brief or "data directory" in brief.lower()
    # The Fix-A smoke fix is programmatic — brief should mention it
    assert "programmatic" in brief.lower() or "apply this with a single click" in brief.lower()


def test_help_brief_for_component_includes_diagnostic_section():
    from work_buddy.control.help_briefs import build_help_brief
    brief = build_help_brief("component:obsidian")
    assert "obsidian" in brief.lower()
    assert "diagnostic" in brief.lower()


# ---------------------------------------------------------------------------
# Endpoint integration via Flask test client
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from work_buddy.dashboard.service import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_fix_endpoint_404_unknown_returns_ok_false_payload(client):
    """The endpoint never 404s on unknown req_ids — the dispatcher
    returns a structured ok:false payload so the UI can show a toast
    instead of an opaque HTTP error."""
    with mock.patch("work_buddy.dashboard.service._is_read_only", return_value=False):
        resp = client.post("/api/control/fix/totally/fake/id", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False


def test_fix_endpoint_blocked_in_read_only(client):
    with mock.patch("work_buddy.dashboard.service._is_read_only", return_value=True):
        resp = client.post("/api/control/fix/core/data/writable", json={})
    assert resp.status_code == 403


def test_fix_endpoint_smoke_e2e(client, tmp_path, monkeypatch):
    """End-to-end via Flask: POST to the smoke fix and get back recheck data."""
    target = tmp_path / "data"
    monkeypatch.setattr("work_buddy.paths.data_dir", lambda *a, **k: target)
    with mock.patch("work_buddy.dashboard.service._is_read_only", return_value=False), \
         mock.patch("work_buddy.consent.grant_consent"):
        resp = client.post("/api/control/fix/core/data/writable", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert target.exists()


def test_help_endpoint_blocked_in_read_only(client):
    with mock.patch("work_buddy.dashboard.service._is_read_only", return_value=True):
        resp = client.post("/api/control/help/component:obsidian")
    assert resp.status_code == 403


def test_help_endpoint_dispatches(client):
    """The endpoint calls into help_briefs.spawn_help_agent."""
    with mock.patch("work_buddy.dashboard.service._is_read_only", return_value=False), \
         mock.patch(
             "work_buddy.control.help_briefs.spawn_help_agent",
             return_value={"ok": True, "detail": "ok", "session_id": "s", "pid": 1, "message": "ok"},
         ) as mock_spawn:
        resp = client.post("/api/control/help/component:obsidian")
    assert resp.status_code == 200
    assert mock_spawn.called
    data = resp.get_json()
    assert data["ok"] is True
