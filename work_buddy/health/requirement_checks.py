"""Requirement check functions — configuration-time validation.

Each function returns ``{"ok": bool, "detail": str}``.

These are fast, deterministic checks — filesystem and config inspection only.
No HTTP calls, no service pings, no bridge communication.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any


def _cfg() -> dict[str, Any]:
    """Load config lazily to avoid circular imports at module level."""
    from work_buddy.config import load_config
    return load_config()


def _vault_root() -> Path:
    """Resolve vault_root from config."""
    return Path(_cfg().get("vault_root", ""))


def _repo_root() -> Path:
    """Work-buddy repo root."""
    return Path(__file__).parent.parent.parent


# ---------------------------------------------------------------------------
# Core / bootstrap checks
# ---------------------------------------------------------------------------


def check_config_yaml_exists() -> dict[str, Any]:
    """Check that config.yaml exists in the repo root."""
    path = _repo_root() / "config.yaml"
    if path.exists():
        return {"ok": True, "detail": f"config.yaml found at {path}"}
    return {"ok": False, "detail": f"config.yaml not found at {path}"}


def check_config_local_exists() -> dict[str, Any]:
    """Check that config.local.yaml exists."""
    path = _repo_root() / "config.local.yaml"
    if path.exists():
        return {"ok": True, "detail": f"config.local.yaml found at {path}"}
    example = _repo_root() / "config.local.yaml.example"
    hint = " (config.local.yaml.example exists — copy it)" if example.exists() else ""
    return {"ok": False, "detail": f"config.local.yaml not found{hint}"}


def check_vault_root() -> dict[str, Any]:
    """Check that vault_root is set and points to an existing directory."""
    cfg = _cfg()
    vault_root = cfg.get("vault_root", "")
    if not vault_root:
        return {"ok": False, "detail": "vault_root is empty or not set in config"}
    p = Path(vault_root)
    if p.is_dir():
        return {"ok": True, "detail": f"vault_root exists: {p}"}
    return {"ok": False, "detail": f"vault_root does not exist: {p}"}


def check_repos_root() -> dict[str, Any]:
    """Check that repos_root is set and points to an existing directory."""
    cfg = _cfg()
    repos_root = cfg.get("repos_root", "")
    if not repos_root:
        return {"ok": False, "detail": "repos_root is empty or not set in config"}
    p = Path(repos_root)
    if p.is_dir():
        return {"ok": True, "detail": f"repos_root exists: {p}"}
    return {"ok": False, "detail": f"repos_root does not exist: {p}"}


def check_timezone() -> dict[str, Any]:
    """Check that timezone is a valid IANA timezone."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    cfg = _cfg()
    tz = cfg.get("timezone", "")
    if not tz:
        return {"ok": False, "detail": "timezone is empty or not set"}
    try:
        ZoneInfo(tz)
        return {"ok": True, "detail": f"Valid timezone: {tz}"}
    except (ZoneInfoNotFoundError, KeyError):
        return {"ok": False, "detail": f"Invalid IANA timezone: '{tz}'"}


def check_anthropic_api_key() -> dict[str, Any]:
    """Check that ANTHROPIC_API_KEY environment variable is set."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return {"ok": True, "detail": f"ANTHROPIC_API_KEY is set ({len(key)} chars)"}
    return {"ok": False, "detail": "ANTHROPIC_API_KEY environment variable is not set"}


def check_data_writable() -> dict[str, Any]:
    """Check that the data/ directory exists and is writable."""
    from work_buddy.paths import data_dir

    try:
        d = data_dir("runtime")
        d.mkdir(parents=True, exist_ok=True)
        # Test actual writability
        test_file = d / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return {"ok": True, "detail": f"data/ is writable ({d.parent})"}
    except Exception as exc:
        return {"ok": False, "detail": f"data/ directory issue: {exc}"}


# ---------------------------------------------------------------------------
# Obsidian vault structure checks
# ---------------------------------------------------------------------------


def check_obsidian_dir() -> dict[str, Any]:
    """Check that .obsidian/ exists in vault root."""
    vault = _vault_root()
    if not vault or not vault.is_dir():
        return {"ok": False, "detail": "vault_root is not set or doesn't exist (run bootstrap checks first)"}
    obs_dir = vault / ".obsidian"
    if obs_dir.is_dir():
        return {"ok": True, "detail": f".obsidian/ found in {vault}"}
    return {"ok": False, "detail": f".obsidian/ not found in {vault}"}


def check_daily_notes_plugin() -> dict[str, Any]:
    """Check that the Daily Notes core plugin is enabled."""
    vault = _vault_root()
    if not vault or not vault.is_dir():
        return {"ok": False, "detail": "vault_root is not set or doesn't exist"}
    # Core plugins are stored in .obsidian/core-plugins-migration.json
    # or .obsidian/core-plugins.json depending on Obsidian version
    for filename in ("core-plugins-migration.json", "core-plugins.json"):
        cp_file = vault / ".obsidian" / filename
        if cp_file.exists():
            try:
                with open(cp_file, encoding="utf-8") as f:
                    data = json.load(f)
                # core-plugins-migration.json: {"daily-notes": true, ...}
                if isinstance(data, dict):
                    if data.get("daily-notes") is True:
                        return {"ok": True, "detail": "Daily Notes core plugin is enabled"}
                    if "daily-notes" in data:
                        return {"ok": False, "detail": "Daily Notes core plugin is disabled"}
                # core-plugins.json: ["daily-notes", ...]
                if isinstance(data, list) and "daily-notes" in data:
                    return {"ok": True, "detail": "Daily Notes core plugin is enabled"}
            except (json.JSONDecodeError, OSError):
                continue
    return {"ok": False, "detail": "Could not determine Daily Notes plugin status"}


def check_journal_dir() -> dict[str, Any]:
    """Check that the journal directory exists."""
    vault = _vault_root()
    cfg = _cfg()
    journal_dir_name = cfg.get("obsidian", {}).get("journal_dir", "journal")
    journal_path = vault / journal_dir_name
    if journal_path.is_dir():
        return {"ok": True, "detail": f"Journal directory exists: {journal_path}"}
    return {"ok": False, "detail": f"Journal directory not found: {journal_path}"}


def _find_todays_note() -> Path | None:
    """Find today's daily note file."""
    from datetime import date
    vault = _vault_root()
    cfg = _cfg()
    journal_dir = cfg.get("obsidian", {}).get("journal_dir", "journal")
    today = date.today().strftime("%Y-%m-%d")
    note_path = vault / journal_dir / f"{today}.md"
    return note_path if note_path.exists() else None


def _note_has_section(header_pattern: str) -> dict[str, Any]:
    """Check if today's note has a section matching the pattern (case-insensitive)."""
    import re
    note = _find_todays_note()
    if note is None:
        return {"ok": False, "detail": "Today's daily note does not exist yet"}
    try:
        content = note.read_text(encoding="utf-8")
        # Match markdown headers, ignoring bold/italic formatting
        pattern = rf"^#+\s+\**{re.escape(header_pattern)}"
        if re.search(pattern, content, re.MULTILINE | re.IGNORECASE):
            return {"ok": True, "detail": f"Found '{header_pattern}' section in {note.name}"}
        return {"ok": False, "detail": f"No '{header_pattern}' section found in {note.name}"}
    except OSError as exc:
        return {"ok": False, "detail": f"Could not read {note}: {exc}"}


def check_log_section() -> dict[str, Any]:
    """Check that today's note has a '# Log' section."""
    return _note_has_section("Log")


def check_sign_in_section() -> dict[str, Any]:
    """Check that today's note has a '# Sign-In' section."""
    return _note_has_section("Sign-In")


def check_running_notes_section() -> dict[str, Any]:
    """Check that today's note has a 'Running Notes' section."""
    return _note_has_section("Running Notes")


def check_master_task_list() -> dict[str, Any]:
    """Check that the master task list file exists."""
    vault = _vault_root()
    task_file = vault / "tasks" / "master-task-list.md"
    if task_file.exists():
        return {"ok": True, "detail": f"Master task list found: {task_file}"}
    return {"ok": False, "detail": f"Master task list not found: {task_file}"}


def check_tasks_plugin() -> dict[str, Any]:
    """Check that the Obsidian Tasks community plugin is installed and enabled.

    Uses direct file inspection rather than importing plugins.py to avoid
    triggering agent_session side-effects.
    """
    vault = _vault_root()
    if not vault or not vault.is_dir():
        return {"ok": False, "detail": "vault_root is not set or doesn't exist"}
    cp_file = vault / ".obsidian" / "community-plugins.json"
    if not cp_file.exists():
        return {"ok": False, "detail": "community-plugins.json not found"}
    try:
        with open(cp_file, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and "obsidian-tasks-plugin" in data:
            return {"ok": True, "detail": "Tasks plugin is active"}
        return {"ok": False, "detail": "Tasks plugin is not in community-plugins.json"}
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "detail": f"Could not read community-plugins.json: {exc}"}


def check_contracts_dir() -> dict[str, Any]:
    """Check that the contracts directory exists in the vault."""
    vault = _vault_root()
    cfg = _cfg()
    contracts_path = cfg.get("contracts", {}).get("vault_path", "work-buddy/contracts")
    full_path = vault / contracts_path
    if full_path.is_dir():
        return {"ok": True, "detail": f"Contracts directory found: {full_path}"}
    return {"ok": False, "detail": f"Contracts directory not found: {full_path}"}


def check_personal_knowledge_path() -> dict[str, Any]:
    """Check that the personal knowledge vault path exists."""
    vault = _vault_root()
    cfg = _cfg()
    pk_path = cfg.get("personal_knowledge", {}).get("vault_path", "Meta/WorkBuddy")
    full_path = vault / pk_path
    if full_path.is_dir():
        return {"ok": True, "detail": f"Personal knowledge path found: {full_path}"}
    return {"ok": False, "detail": f"Personal knowledge path not found: {full_path}"}


# ---------------------------------------------------------------------------
# Integration checks
# ---------------------------------------------------------------------------


def check_pg_scheduled_task() -> dict[str, Any]:
    """Check Windows scheduled task for PostgreSQL (Windows-only)."""
    if platform.system() != "Windows":
        return {"ok": True, "detail": "Not Windows — skipping scheduled task check"}
    try:
        result = subprocess.run(
            ["powershell.exe", "-Command",
             "Get-ScheduledTask -TaskName 'Hindsight-PostgreSQL' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty TaskName"],
            capture_output=True, text=True, timeout=10,
        )
        if "Hindsight-PostgreSQL" in result.stdout:
            return {"ok": True, "detail": "Windows scheduled task 'Hindsight-PostgreSQL' exists"}
        return {"ok": False, "detail": "Windows scheduled task 'Hindsight-PostgreSQL' not found"}
    except Exception as exc:
        return {"ok": False, "detail": f"Could not check scheduled tasks: {exc}"}


def check_chrome_native_host() -> dict[str, Any]:
    """Check Chrome native messaging host manifest is registered.

    The manifest can be named either ``com.work_buddy.tabs.json`` or
    ``work_buddy_tabs.json`` depending on install method.
    """
    manifest_names = ("com.work_buddy.tabs.json", "work_buddy_tabs.json")

    if platform.system() == "Windows":
        search_dirs = [
            Path(os.environ.get("APPDATA", "")) / "Google" / "Chrome" / "NativeMessagingHosts",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data" / "NativeMessagingHosts",
        ]
    elif platform.system() == "Darwin":
        search_dirs = [
            Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts",
        ]
    else:
        search_dirs = [
            Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts",
        ]

    for d in search_dirs:
        for name in manifest_names:
            manifest = d / name
            if manifest.exists():
                return {"ok": True, "detail": f"Native host manifest found: {manifest}"}

    return {"ok": False, "detail": "Native host manifest not found in Chrome NativeMessagingHosts"}


# ---------------------------------------------------------------------------
# Service checks
# ---------------------------------------------------------------------------


def check_telegram_bot_token() -> dict[str, Any]:
    """Check that the Telegram bot token is configured."""
    cfg = _cfg()
    token_env = cfg.get("telegram", {}).get("bot_token_env", "TELEGRAM_BOT_TOKEN")
    if os.environ.get(token_env):
        return {"ok": True, "detail": f"${token_env} is set"}
    # Check .env file in repo root
    env_file = _repo_root() / ".env"
    if env_file.exists():
        try:
            content = env_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line.startswith(f"{token_env}=") and len(line.split("=", 1)[1].strip()) > 0:
                    return {"ok": True, "detail": f"${token_env} found in .env file"}
        except OSError:
            pass
    return {"ok": False, "detail": f"${token_env} not found in environment or .env file"}
