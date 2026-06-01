"""Configuration loading for work-buddy context bundle collector."""

import copy
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

logger = logging.getLogger(__name__)

# Memoized parsed YAML, keyed on file path → (mtime_ns, parsed dict). The
# config files don't change for the life of a process (except via
# ``write_config_local``, which bumps the mtime and so invalidates the
# entry). ``load_config`` runs on the hot path of every store's
# ``get_connection`` via ``_db_path``; an unmemoized parse put ~45ms of
# PyYAML on every DB open across the app. A runtime edit is picked up
# because the cache key is the file mtime.
_PARSED_YAML_CACHE: dict[str, tuple[int, dict[str, Any]]] = {}


def _read_yaml_memoized(path: Path) -> dict[str, Any]:
    """Parse ``path`` as YAML, memoized on its mtime.

    Returns a deep copy so neither callers nor ``_deep_merge`` (which
    shares override leaves by reference) can mutate the cached parse.
    Missing or unreadable files yield ``{}``.
    """
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _PARSED_YAML_CACHE.get(str(path))
    if cached is None or cached[0] != mtime:
        try:
            with open(path) as f:
                parsed = yaml.safe_load(f) or {}
        except Exception:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        cached = (mtime, parsed)
        _PARSED_YAML_CACHE[str(path)] = cached
    return copy.deepcopy(cached[1])


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
        # Disabled by default — opt-in via config.local.yaml. Most users won't
        # have Thunderbird + the thunderbird-work-buddy extension installed.
        "thunderbird": {"enabled": False},
    },
    # Email integration. The default provider is "thunderbird" (talks HTTP
    # to the thunderbird-work-buddy companion extension). Set "fake" for
    # tests that exercise the pipeline without any local mail client.
    "email": {
        "enabled": True,
        "provider": "thunderbird",
        "candidate_days_back": 2,
        "max_messages": 50,
        "unread_only": True,
        "include_body_chars": 4000,
    },
    "thunderbird": {
        "timeout_seconds": 10,
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
    "projects": {
        # Single vault directory holding one markdown note per project
        # (the markdown-canonical surface — see architecture/markdown-db).
        # Vault-relative; sibling to contracts.vault_path. Surfaced as a
        # Repository-Setup requirement so it is user-configurable.
        "markdown_dir": "work-buddy/projects",
    },
    "workflows": {
        # Lifecycle of in-flight workflow runs in the MCP gateway's
        # in-memory active-runs map. See work_buddy/mcp_server/conductor.py.
        "run_lifecycle": {
            "idle_timeout_hours": 24,
            "sweep_interval_minutes": 60,
            "recovery_enabled": True,
        },
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

    # Parses are memoized on file mtime (see ``_read_yaml_memoized``);
    # the merge itself is cheap and still produces a fresh dict per call.
    user_cfg = _read_yaml_memoized(config_path)
    if user_cfg:
        cfg = _deep_merge(cfg, user_cfg)

    # Local overrides (gitignored, machine-specific)
    local_cfg = _read_yaml_memoized(config_path.with_name("config.local.yaml"))
    if local_cfg:
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


def safe_timezone(name: str | None, *, fallback: str = "UTC") -> str:
    """Return ``name`` if it is a valid IANA timezone, else ``fallback``.

    Runtime code (the scheduler, display formatting) constructs
    ``ZoneInfo`` from the configured timezone. An invalid value would
    otherwise raise every time it is used — silently halting the
    scheduler tick. Validate once here and degrade to a safe zone
    instead of throwing; a set-but-invalid value logs a warning.
    """
    if not name:
        return fallback
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        logger.warning(
            "Invalid timezone %r in config; falling back to %s", name, fallback
        )
        return fallback


_USER_TZ_CACHE: ZoneInfo | None = None


def _compute_user_tz() -> ZoneInfo:
    cfg = load_config()
    return ZoneInfo(safe_timezone(cfg.get("timezone")))


def __getattr__(name: str):
    """PEP 562 module-level getattr: compute USER_TZ lazily on first access.

    Deferring config load from import time to first-use shaves ~100ms off
    the mcp_gateway boot path. Consumers keep using
    ``from work_buddy.config import USER_TZ`` — the attribute
    materializes and caches on first read.
    """
    global _USER_TZ_CACHE
    if name == "USER_TZ":
        if _USER_TZ_CACHE is None:
            _USER_TZ_CACHE = _compute_user_tz()
        return _USER_TZ_CACHE
    raise AttributeError(f"module 'work_buddy.config' has no attribute {name!r}")
