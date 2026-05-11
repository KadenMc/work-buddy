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


def _obsidian_config_dir(vault: Path | None = None) -> Path:
    """Resolve the Obsidian config dir for the current vault.

    Honors ``obsidian.config_dir`` in config.yaml so vaults using
    Obsidian's override-config-folder feature (not ``.obsidian``) are
    still correctly inspected. Falls back to ``<vault>/.obsidian``.
    Deferred import of the plugin helper avoids cross-package
    circular-import risk at module load time.
    """
    from work_buddy.obsidian.plugins import resolve_config_dir
    return resolve_config_dir(vault if vault is not None else _vault_root())


# ---------------------------------------------------------------------------
# Core / bootstrap checks
# ---------------------------------------------------------------------------


def check_lmstudio_reachable() -> dict[str, Any]:
    """Check LM Studio's local server is reachable.

    Only meaningful when the user has opted into offloading at least
    one embedding model to LM Studio (``embedding.models.<key>.provider:
    lmstudio`` in config). Delegates to the same runtime health probe
    used by the embedding component's check_sequence so both read from
    one place — ``work_buddy.health.checks.check_lmstudio``.

    If the user hasn't opted in to any LM Studio provider, this check
    is still registered (the user may opt in later) but it's marked
    ``severity=recommended`` at the RequirementDef level so failures
    don't block setup.
    """
    from work_buddy.health.checks import check_lmstudio
    return check_lmstudio()


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
    """Check that vault_root is set, exists, AND is actually an Obsidian vault.

    "Obsidian vault" is defined as "a directory containing a `.obsidian/`
    subdirectory" — that's the marker Obsidian itself uses. We fold this
    check into vault_root rather than a separate requirement because
    vault_root's semantic meaning IS "path to an Obsidian vault." A
    directory without `.obsidian/` isn't a vault, so vault_root is wrong;
    splitting that into two requirements muddied the diagnostic.
    """
    cfg = _cfg()
    vault_root = cfg.get("vault_root", "")
    if not vault_root:
        return {"ok": False, "detail": "vault_root is empty or not set in config"}
    p = Path(vault_root)
    if not p.is_dir():
        return {"ok": False, "detail": f"vault_root does not exist: {p}"}
    config_dir = _obsidian_config_dir(p)
    if not config_dir.is_dir():
        expected = config_dir.name
        return {
            "ok": False,
            "detail": (
                f"{p} exists but isn't an Obsidian vault — no {expected}/ "
                "subdirectory. Either point vault_root at the correct "
                "directory, open this directory in Obsidian once so it "
                "initializes the config folder, OR set "
                "obsidian.config_dir in config.yaml if your vault uses "
                "Obsidian's override-config-folder feature with a "
                "non-default name."
            ),
        }
    return {"ok": True, "detail": f"vault_root is a valid Obsidian vault: {p}"}


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
    """Check that the Anthropic API key is reachable from one of the
    sources ``work_buddy.llm.runner`` actually consults.

    Mirrors `runner.py:214` precisely:

      1. ``SUBAGENT_ANTHROPIC_API_KEY`` env var (preferred — set this
         in environments where ``ANTHROPIC_API_KEY`` is intentionally
         absent so agent spawns fall back to OAuth/Claude Max).
      2. ``ANTHROPIC_API_KEY`` env var (fallback — also activates API
         billing for spawned Claude Code sessions, see executor.py).
      3. ``.env`` file at the repo root, scanned for either of the
         above keys.

    The previous version checked only ``ANTHROPIC_API_KEY`` and missed
    the dedicated subagent key, marking the requirement failed even
    when LLM calls were actually working fine.
    """
    sub_key = os.environ.get("SUBAGENT_ANTHROPIC_API_KEY", "")
    if sub_key:
        return {
            "ok": True,
            "detail": f"SUBAGENT_ANTHROPIC_API_KEY is set ({len(sub_key)} chars)",
        }
    main_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if main_key:
        return {
            "ok": True,
            "detail": f"ANTHROPIC_API_KEY is set ({len(main_key)} chars)",
        }

    # Fall back to scanning the repo .env file — runner.py does this too.
    repo_env = _repo_root() / ".env"
    if repo_env.exists():
        try:
            for line in repo_env.read_text(encoding="utf-8").splitlines():
                if line.startswith("SUBAGENT_ANTHROPIC_API_KEY="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return {
                            "ok": True,
                            "detail": f"SUBAGENT_ANTHROPIC_API_KEY in .env ({len(val)} chars)",
                        }
                if line.startswith("ANTHROPIC_API_KEY="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return {
                            "ok": True,
                            "detail": f"ANTHROPIC_API_KEY in .env ({len(val)} chars)",
                        }
        except OSError as exc:
            return {"ok": False, "detail": f"Could not read .env: {exc}"}

    return {
        "ok": False,
        "detail": (
            "No Anthropic API key found — set SUBAGENT_ANTHROPIC_API_KEY "
            "or ANTHROPIC_API_KEY (env var or .env file)."
        ),
    }


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


def check_daily_notes_plugin() -> dict[str, Any]:
    """Check that the Daily Notes core plugin is enabled."""
    vault = _vault_root()
    if not vault or not vault.is_dir():
        return {"ok": False, "detail": "vault_root is not set or doesn't exist"}
    # Core plugins are stored in <config>/core-plugins-migration.json
    # or <config>/core-plugins.json depending on Obsidian version.
    # <config> is usually .obsidian but may be overridden via
    # obsidian.config_dir in config.yaml.
    config_dir = _obsidian_config_dir(vault)
    for filename in ("core-plugins-migration.json", "core-plugins.json"):
        cp_file = config_dir / filename
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
    """Find today's daily note file (strict — only today)."""
    from datetime import date
    vault = _vault_root()
    cfg = _cfg()
    journal_dir = cfg.get("obsidian", {}).get("journal_dir", "journal")
    today = date.today().strftime("%Y-%m-%d")
    note_path = vault / journal_dir / f"{today}.md"
    return note_path if note_path.exists() else None


def _find_latest_daily_note() -> tuple[Path | None, str | None]:
    """Find today's daily note, falling back to the most recent earlier one.

    Returns ``(path, is_today_flag)`` where ``is_today_flag`` is the
    date string actually used (``"today"`` if the match was today's
    file, or an ISO date otherwise). Returns ``(None, None)`` if no
    daily note exists at all within the last 30 days.

    Rationale: the user's day doesn't begin at midnight — they often
    work past midnight against yesterday's note before sleeping and
    creating the next day's. A strict "today only" check marks the
    daily-note sections as degraded every post-midnight session, which
    is noise, not signal. Fall back to the latest available note and
    tell the user which date we're validating.
    """
    from datetime import date, timedelta
    import re as _re

    vault = _vault_root()
    cfg = _cfg()
    journal_dir = cfg.get("obsidian", {}).get("journal_dir", "journal")
    journal_path = vault / journal_dir

    today = date.today()
    today_str = today.strftime("%Y-%m-%d")
    today_path = journal_path / f"{today_str}.md"
    if today_path.exists():
        return today_path, "today"

    # Scan the past 30 days for a dated daily-note file. 30 is a
    # generous-but-bounded window — if nothing exists there, the user
    # genuinely has no recent daily note and the check should report it.
    for i in range(1, 31):
        d = today - timedelta(days=i)
        candidate = journal_path / f"{d.strftime('%Y-%m-%d')}.md"
        if candidate.exists():
            return candidate, d.strftime("%Y-%m-%d")
    return None, None


def _note_has_section(header_pattern: str) -> dict[str, Any]:
    """Check whether the latest daily note has a section.

    Uses ``_find_latest_daily_note`` so a post-midnight session with no
    today-dated file yet still validates against yesterday's note. The
    detail string surfaces which date was actually checked.
    """
    import re
    note, which_date = _find_latest_daily_note()
    if note is None:
        return {
            "ok": False,
            "detail": (
                "No daily note found within the last 30 days — "
                "create one to validate journal sections."
            ),
        }
    try:
        content = note.read_text(encoding="utf-8")
        # Match markdown headers, ignoring bold/italic formatting
        pattern = rf"^#+\s+\**{re.escape(header_pattern)}"
        found = bool(re.search(pattern, content, re.MULTILINE | re.IGNORECASE))
        prefix = (
            ""
            if which_date == "today"
            else f"Operating on last available daily note ({which_date}). "
        )
        if found:
            return {
                "ok": True,
                "detail": f"{prefix}Found '{header_pattern}' section in {note.name}",
            }
        return {
            "ok": False,
            "detail": f"{prefix}No '{header_pattern}' section found in {note.name}",
        }
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


def _community_plugin_state(
    *,
    plugin_id: str,
    folder_name: str,
    install_hint: str,
    enable_hint: str,
    healthy_detail: str,
) -> dict[str, Any]:
    """Shared two-part community-plugin check: installed vs enabled.

    Every Obsidian community plugin lives in two orthogonal states that
    users tend to conflate:

      * **Installed** — a folder ``.obsidian/plugins/<folder_name>/``
        exists with a ``manifest.json`` inside. This proves the user
        has the plugin code on disk.
      * **Enabled**   — the plugin's id appears in
        ``.obsidian/community-plugins.json`` (a bare JSON list of the
        enabled-plugin ids Obsidian loads on startup). This proves the
        user has toggled it on.

    The two can disagree in both directions: installed-but-disabled is
    common (user turned it off); enabled-without-install is rare but
    not impossible (stale config file). The previous ``check_tasks_plugin``
    conflated both failure modes under a single "not in
    community-plugins.json" message, so the user couldn't tell whether
    to install or just toggle on.

    Returns a RequirementResult-shaped dict. The detail message is
    specific to which state we landed in:
      * ok:           {healthy_detail}
      * not installed: {install_hint}
      * installed but disabled: {enable_hint}
      * missing community-plugins.json: "enable any community plugin first"
      * malformed json / IO error: the raw OSError message
    """
    vault = _vault_root()
    if not vault or not vault.is_dir():
        return {"ok": False, "detail": "vault_root is not set or doesn't exist"}

    config_dir = _obsidian_config_dir(vault)
    plugin_dir = config_dir / "plugins" / folder_name
    manifest = plugin_dir / "manifest.json"
    if not manifest.exists():
        return {
            "ok": False,
            "detail": f"Plugin NOT installed at {plugin_dir}. {install_hint}",
        }

    cp_file = config_dir / "community-plugins.json"
    if not cp_file.exists():
        return {
            "ok": False,
            "detail": (
                "Plugin is installed, but community-plugins.json is missing — "
                "Obsidian hasn't been opened with any community plugin enabled. "
                f"{enable_hint}"
            ),
        }
    try:
        with open(cp_file, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "detail": f"Could not read community-plugins.json: {exc}"}

    if isinstance(data, list) and plugin_id in data:
        return {"ok": True, "detail": healthy_detail}

    # Installed but not enabled — the most common failure mode, and the
    # one the user can fix in seconds.
    return {
        "ok": False,
        "detail": f"Plugin IS installed but NOT enabled. {enable_hint}",
    }


def check_tasks_plugin() -> dict[str, Any]:
    """Check that the Obsidian Tasks community plugin is installed AND enabled.

    Distinguishes installed-but-disabled (toggle it on) from not-
    installed (install first). Uses direct file inspection rather than
    importing plugins.py to avoid triggering agent_session side-effects.
    """
    return _community_plugin_state(
        plugin_id="obsidian-tasks-plugin",
        folder_name="obsidian-tasks-plugin",
        install_hint=(
            "Install via Obsidian Settings → Community Plugins → Browse, "
            "search for 'Tasks' (by Martin Schenck)."
        ),
        enable_hint=(
            "Open Obsidian → Settings → Community Plugins and toggle "
            "'Tasks' on."
        ),
        healthy_detail="Tasks plugin installed and enabled",
    )


def get_work_buddy_plugin_state() -> tuple[str, str]:
    """Return a short state code + detail for the work-buddy plugin.

    Filesystem-only inspection (no network). Safe to call from the
    bridge write path to disambiguate connection-refused failures into
    one of the documented four states (see ``obsidian/bridge``
    directions).

    Returns a tuple ``(state, detail)`` where ``state`` is one of:

    * ``"ok"`` — manifest exists and ``work-buddy`` is in
      ``community-plugins.json``. Bridge should be reachable; a
      connection-refused error here means state 1 (Obsidian not running)
      or state 2 (bridge lagging under load).
    * ``"not_installed"`` — plugin folder/manifest missing (state 3).
    * ``"disabled"`` — manifest present but ``work-buddy`` absent from
      ``community-plugins.json`` (state 4).
    * ``"unknown"`` — vault misconfigured, config dir unreadable, or IO
      error. Diagnostics in ``detail``.
    """
    vault = _vault_root()
    if not vault or not vault.is_dir():
        return "unknown", "vault_root is not set or doesn't exist"

    config_dir = _obsidian_config_dir(vault)
    plugin_dir = config_dir / "plugins" / "obsidian-work-buddy"
    manifest = plugin_dir / "manifest.json"
    if not manifest.exists():
        return "not_installed", f"plugin missing at {plugin_dir}"

    cp_file = config_dir / "community-plugins.json"
    if not cp_file.exists():
        return "disabled", "community-plugins.json missing — no community plugins enabled"
    try:
        with open(cp_file, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return "unknown", f"could not read community-plugins.json: {exc}"

    if isinstance(data, list) and "work-buddy" in data:
        return "ok", "installed and enabled"
    return "disabled", "installed but not enabled in community-plugins.json"


def check_work_buddy_plugin() -> dict[str, Any]:
    """Check that the work-buddy Obsidian plugin is installed AND enabled.

    The plugin (https://github.com/KadenMc/obsidian-work-buddy) is what
    provides the Obsidian bridge HTTP endpoint on port 27125 that the
    ``obsidian`` tool probe and every bridge-backed capability depends
    on. Delegates to ``_community_plugin_state`` for the two-part
    installed-vs-enabled distinction.
    """
    vault = _vault_root()
    if not vault or not vault.is_dir():
        return {"ok": False, "detail": "vault_root is not set or doesn't exist"}

    config_dir = _obsidian_config_dir(vault)
    plugin_dir = config_dir / "plugins" / "obsidian-work-buddy"
    manifest = plugin_dir / "manifest.json"
    if not manifest.exists():
        return {
            "ok": False,
            "detail": (
                f"work-buddy plugin NOT installed at {plugin_dir} — "
                "clone https://github.com/KadenMc/obsidian-work-buddy into "
                f"{config_dir.name}/plugins/ and enable it under Settings → "
                "Community Plugins."
            ),
        }

    cp_file = config_dir / "community-plugins.json"
    if not cp_file.exists():
        return {
            "ok": False,
            "detail": (
                "community-plugins.json not found — enable at least one "
                "community plugin in Obsidian first."
            ),
        }
    try:
        with open(cp_file, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "detail": f"Could not read community-plugins.json: {exc}"}

    if isinstance(data, list) and "work-buddy" in data:
        return {
            "ok": True,
            "detail": "work-buddy plugin installed and enabled (bridge should respond on port 27125)",
        }
    return {
        "ok": False,
        "detail": (
            "work-buddy plugin IS installed but NOT enabled — "
            "open Obsidian → Settings → Community Plugins and toggle "
            "'Work Buddy' on."
        ),
    }


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


def check_thunderbird_bridge() -> dict[str, Any]:
    """Check the thunderbird-work-buddy companion bridge is reachable.

    Wraps :func:`work_buddy.email.providers.thunderbird.probe_thunderbird_bridge`
    so the wizard surfaces actionable diagnostics: connection-file missing,
    port closed, 403 (stale token), no allowed accounts. This requirement is
    only meaningful when the user has opted into the bridge by setting
    ``tools.thunderbird.enabled: true`` — when off, we report ``ok: True``
    with an "(opted out)" detail so the wizard doesn't nag people who don't
    use Thunderbird.
    """
    cfg = _cfg()
    if not cfg.get("tools", {}).get("thunderbird", {}).get("enabled", False):
        return {
            "ok": True,
            "detail": (
                "Bridge probe skipped: tools.thunderbird.enabled is false "
                "(opt in via config.local.yaml once thunderbird-work-buddy "
                "is installed)."
            ),
        }
    try:
        from work_buddy.email.providers.thunderbird import probe_thunderbird_bridge
    except ImportError as exc:
        return {"ok": False, "detail": f"work_buddy.email module not importable: {exc}"}
    available, reason = probe_thunderbird_bridge()
    return {"ok": bool(available), "detail": reason or ("ok" if available else "not reachable")}


# ---------------------------------------------------------------------------
# Tailscale (remote access)
# ---------------------------------------------------------------------------
#
# Both checks delegate to the shared helper in ``work_buddy.health.checks``
# (the same one the ``tailscale_status`` MCP capability uses), mirroring
# the ``check_lmstudio_reachable`` pattern. The helper memoizes briefly so
# running these alongside the component health checks doesn't trigger
# multiple ``tailscale`` subprocess invocations within one diagnose pass.


def check_tailscale_installed() -> dict[str, Any]:
    """Tailscale CLI is installed and on PATH."""
    from work_buddy.health.checks import get_tailscale_status
    status = get_tailscale_status()
    if not status.get("installed"):
        return {
            "ok": False,
            "detail": (
                "tailscale CLI not found on PATH. Install from "
                "https://tailscale.com/download (Windows installer registers "
                "it under C:\\Program Files\\Tailscale\\)."
            ),
        }
    return {"ok": True, "detail": "tailscale CLI present"}


def check_tailscale_serve_configured() -> dict[str, Any]:
    """Tailscale Serve publishes the dashboard on HTTPS.

    Looks for any Web handler whose Proxy points at ``127.0.0.1:<port>``
    where ``<port>`` matches the configured dashboard port. We don't pin
    the published hostname — Tailscale picks it from the device's tailnet
    DNS name and a user re-naming the device shouldn't fail this check.
    """
    from work_buddy.health.checks import get_tailscale_status

    status = get_tailscale_status()
    if not status.get("installed"):
        return {"ok": False, "detail": "tailscale not installed (see integrations/tailscale/installed)"}
    if not status.get("running"):
        return {
            "ok": False,
            "detail": "tailscale daemon not running — Serve config can't be queried",
        }

    serve = status.get("serve") or {}
    web = serve.get("Web") or {}
    if not web:
        return {
            "ok": False,
            "detail": "tailscale serve has no Web handlers configured",
        }

    dashboard_port = (
        _cfg().get("sidecar", {})
        .get("services", {})
        .get("dashboard", {})
        .get("port", 5127)
    )
    target = f"127.0.0.1:{dashboard_port}"

    for hostname, host_cfg in web.items():
        for path, handler in (host_cfg.get("Handlers") or {}).items():
            proxy = handler.get("Proxy", "")
            if target in proxy:
                return {
                    "ok": True,
                    "detail": f"published at https://{hostname}{path} -> {proxy}",
                }

    return {
        "ok": False,
        "detail": (
            f"no tailscale serve handler proxies to {target}. "
            f"Run `tailscale serve --bg {dashboard_port}` (or click Fix)."
        ),
    }


# ---------------------------------------------------------------------------
# github_backups
# ---------------------------------------------------------------------------


def check_gh_cli_installed() -> dict[str, Any]:
    """Probe for ``gh`` on PATH via ``gh --version``."""
    try:
        proc = subprocess.run(
            ["gh", "--version"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return {"ok": False, "detail": "gh CLI not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "gh --version timed out"}
    if proc.returncode != 0:
        return {"ok": False, "detail": f"gh --version returned {proc.returncode}"}
    # First line is "gh version X.Y.Z (<date>)"
    version_line = proc.stdout.strip().splitlines()[0] if proc.stdout else "?"
    return {"ok": True, "detail": version_line}


def check_gh_authenticated() -> dict[str, Any]:
    """Probe authentication state via the remote.probe_gh helper.

    Imports the helper lazily — it lives in ``work_buddy.backups.remote``
    and we don't want a circular import in this hot health-check
    code path.
    """
    try:
        from work_buddy.backups.remote import probe_gh
    except Exception as exc:
        return {"ok": False, "detail": f"could not import probe_gh: {exc}"}
    probe = probe_gh()
    if not probe.get("installed"):
        return {"ok": False, "detail": "gh CLI not installed"}
    if not probe.get("authenticated"):
        return {
            "ok": False,
            "detail": probe.get("error") or "gh CLI not authenticated. "
            "Run `gh auth login` to log in.",
        }
    return {
        "ok": True,
        "detail": (
            f"authenticated as {probe.get('account', '?')} on "
            f"{probe.get('host', '?')}"
        ),
    }


def check_backup_repo_configured() -> dict[str, Any]:
    """Verify ``backups.github.repo`` is set in config AND the repo
    exists on GitHub (via ``gh repo view``).
    """
    try:
        from work_buddy.backups.remote import get_backup_repo
    except Exception as exc:
        return {"ok": False, "detail": f"could not import get_backup_repo: {exc}"}
    repo = get_backup_repo()
    if not repo:
        return {
            "ok": False,
            "detail": "backups.github.repo not set in config.local.yaml",
        }
    # Probe existence with `gh repo view <repo>`. Doesn't fetch anything
    # heavy — just confirms the repo is reachable to the authenticated
    # account.
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", repo, "--json", "name"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return {"ok": False,
                "detail": f"gh not installed; can't verify repo {repo}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "gh repo view timed out"}
    if proc.returncode == 0:
        return {"ok": True, "detail": f"repo {repo} reachable"}
    stderr = (proc.stderr or "").strip()
    if "could not resolve" in stderr.lower() or "not found" in stderr.lower():
        return {
            "ok": False,
            "detail": (
                f"backups.github.repo is set to {repo!r} but the repo "
                "doesn't exist on GitHub. Click Fix to create it "
                "(private, no extra cost)."
            ),
        }
    return {"ok": False, "detail": f"gh repo view failed: {stderr}"}
