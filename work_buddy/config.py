"""Configuration loading for work-buddy context bundle collector."""

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


DEFAULTS = {
    "vault_root": "",   # REQUIRED: set in config.yaml (e.g. "/home/me/MyVault")
    "repos_root": "",   # REQUIRED: set in config.yaml (e.g. "/home/me/repos")
    "bundles_dir": "bundles",
    "timezone": "America/New_York",
    "git": {
        "active_days": 30,
        "detail_days": 7,
        "max_commits": 20,
    },
    "obsidian": {
        "journal_dir": "journal",
        "journal_days": 7,
        "recent_modified_days": 3,
        "tracked_folders": ["tasks", "personal"],
        "exclude_folders": [
            ".obsidian", ".trash", ".specstory", ".git",
            ".makemd", ".space", ".smart-env", ".cursor",
            "repos", "node_modules", "public",
            "Google Keep", "recovery-codes",
        ],
        "bridge_port": 27125,
    },
    "chats": {
        "specstory_days": 7,
        "claude_history_days": 7,
    },
    "dashboard": {
        "read_only": False,  # disable mutating actions (investigate, palette execute, etc.)
        "external_url": "",  # Tailscale HTTPS URL (e.g. "https://machine.tailnet.ts.net")
    },
    "sidecar": {
        "health_check_interval": 30,  # seconds between health checks
        "max_service_crashes": 5,     # give up restarting after this many
        "restart_backoff_base": 5,    # seconds, doubled each crash
        "services": {
            "messaging": {
                "module": "work_buddy.messaging.service",
                "port": 5123,
                "enabled": True,
            },
            "embedding": {
                "module": "work_buddy.embedding",
                "port": 5124,
                "enabled": True,
            },
        },
        "jobs_dir": "sidecar_jobs",
        "exclusion_windows": [],  # quiet hours — no jobs fire
        "message_poll_interval": 15,  # seconds between message polls
        "agent_spawn": {
            "timeout_seconds": 300,       # max execution time per spawn
            "max_budget_usd": 1.00,       # cost ceiling per spawn (system prompt cache alone is ~$0.05)
            "model": "sonnet",            # claude model for spawned agents
            "default_spawn_mode": "headless_ephemeral",  # default for jobs without explicit mode
        },
    },
    "tools": {
        "obsidian": {"enabled": True},
        "chrome_extension": {"enabled": True},
        "hindsight": {"enabled": True},
        "telegram": {"enabled": True},
        "smart_connections": {"enabled": True},
        "embedding": {"enabled": True},
        "messaging": {"enabled": True},
        "datacore": {"enabled": True},
        "google_calendar": {"enabled": True},
    },
    "morning": {
        "phases": {
            "yesterday_close": True,
            "context_snapshot": True,
            "calendar": True,
            "task_briefing": True,
            "contract_check": True,
            "blindspot_scan": True,
        },
        "context_hours": 24,
        "blindspot_depth": "light",
        "max_mits": 3,
        "persist_briefing": True,
        "day_planner": {
            "enabled": True,
            "work_hours": [9, 17],
            "default_task_duration": 60,
            "break_interval": 90,
            "break_duration": 15,
            "include_calendar_events": True,
            "calendar_prefix": "[Cal]",
        },
    },
    "personal_knowledge": {
        "enabled": True,
        "vault_path": "Meta/WorkBuddy",   # relative to vault_root
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, preferring override values."""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load config from YAML file, falling back to defaults.

    Merge order: DEFAULTS → config.yaml → config.local.yaml.
    ``config.local.yaml`` is gitignored and holds machine-specific
    overrides (Tailscale URLs, personal tokens, local paths).
    """
    cfg = DEFAULTS.copy()

    if config_path is None:
        # Look relative to the repo root (parent of work_buddy/)
        config_path = Path(__file__).parent.parent / "config.yaml"

    if config_path.exists():
        with open(config_path) as f:
            user_cfg = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, user_cfg)

    # Local overrides (gitignored, machine-specific)
    local_path = config_path.with_name("config.local.yaml")
    if local_path.exists():
        with open(local_path) as f:
            local_cfg = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, local_cfg)

    return cfg


def _repo_root() -> Path:
    """Return the work-buddy repo root (parent of work_buddy/)."""
    return Path(__file__).parent.parent


def config_local_path() -> Path:
    """Return the path to config.local.yaml."""
    return _repo_root() / "config.local.yaml"


def read_config_local() -> dict[str, Any]:
    """Read config.local.yaml, returning empty dict if it doesn't exist."""
    path = config_local_path()
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_config_local(section: str, data: Any) -> None:
    """Write a top-level section in config.local.yaml, preserving other sections.

    If config.local.yaml doesn't exist, creates it.
    """
    path = config_local_path()
    existing = read_config_local()
    existing[section] = data
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=False)


def _init_user_tz() -> ZoneInfo:
    """Compute the user timezone once at import time."""
    cfg = load_config()
    return ZoneInfo(cfg.get("timezone", "America/New_York"))


USER_TZ: ZoneInfo = _init_user_tz()
"""User's timezone from config.yaml — computed once at import time."""
