"""Requirements system — configuration-time validation of hidden assumptions.

Requirements answer "is the environment set up correctly for this subsystem
to work?" — distinct from health checks ("is the service running right now?").

Requirement IDs follow a strict hierarchy: ``{domain}/{subsystem}/{check-name}``

Domains:
    - ``core/`` — Fundamental bootstrap (must pass before anything else)
    - ``obsidian/`` — Vault structure, plugins, templates
    - ``services/`` — Sidecar and external services
    - ``integrations/`` — Chrome, Hindsight, etc.

Example IDs:
    - ``core/config/vault-root``
    - ``obsidian/daily-note/log-section``
    - ``services/telegram/bot-token``
"""

from __future__ import annotations

import importlib
import logging
import platform
from dataclasses import dataclass, field
from typing import Any

_IS_WINDOWS = platform.system() == "Windows"

log = logging.getLogger(__name__)


@dataclass
class RequirementDef:
    """Definition of a configuration-time requirement.

    Attributes:
        id: Hierarchical path, e.g. ``core/config/vault-root``.
        component: Component ID this belongs to, or None for core requirements.
        description: Human-readable description of what's checked.
        check_fn: Dotted import path to callable returning ``{ok, detail}``.
        severity: ``"required"`` or ``"recommended"``.
        fix_hint: Human-readable fix instructions (legacy; new code prefers fix_*).
        setup_group: Wizard grouping: ``"repository"``, ``"journal"``, ``"tasks"``, etc.

    Fix system (added 2026-04-22) — opt-in per requirement:
        fix_kind: How this requirement can be fixed:
            - ``"none"`` (default): no automated fix; user must follow fix_hint manually.
            - ``"programmatic"``: ``fix_fn(**no_args)`` does the fix end-to-end.
            - ``"input_required"``: ``fix_fn(**user_inputs)`` does the fix; user must
              first supply values declared in ``fix_params``.
            - ``"agent_handoff"``: too complex for a button; clicking "Fix" spawns
              a Claude Code session with ``fix_agent_brief`` as context.
        fix_fn: Dotted import path to callable returning ``{ok: bool, detail: str,
                side_effects?: list[str]}``. Required for programmatic / input_required.
        fix_params: For input_required, declares the form fields the user must fill.
            Shape: ``{field_name: {type: "str"|"path"|"secret", label: str,
                                    default: Any, required: bool, hint: str}}``.
        fix_preview: One-line description of what the fix will do, shown in the
            confirm popover before the user commits. E.g. "Will create
            C:\\Vaults\\SecondBrain\\journal\\". Null = no preview shown.
        fix_agent_brief: For agent_handoff, the prompt the spawned Claude Code
            session receives. Should explain what the user is trying to fix and
            what the agent is empowered to do. Null = use a generic brief built
            from the other requirement metadata.
    """

    id: str
    component: str | None
    description: str
    check_fn: str
    severity: str  # "required" | "recommended"
    fix_hint: str
    setup_group: str
    # Fix system (defaults preserve back-compat: existing reqs are no-fix)
    fix_kind: str = "none"  # "none" | "programmatic" | "input_required" | "agent_handoff"
    fix_fn: str | None = None
    fix_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    fix_preview: str | None = None
    fix_agent_brief: str | None = None


@dataclass
class RequirementResult:
    """Result of checking a single requirement."""

    id: str
    ok: bool
    detail: str
    fix_hint: str
    severity: str
    component: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ok": self.ok,
            "detail": self.detail,
            "fix_hint": self.fix_hint if not self.ok else "",
            "severity": self.severity,
            "component": self.component,
        }


# ---------------------------------------------------------------------------
# Requirement registry
# ---------------------------------------------------------------------------

REQUIREMENT_REGISTRY: dict[str, RequirementDef] = {}


def _register(req: RequirementDef) -> None:
    REQUIREMENT_REGISTRY[req.id] = req


def _check_fn_module(check_fn: str) -> str:
    """Module path from a dotted check_fn."""
    return check_fn.rsplit(".", 1)[0]


# --- Bootstrap (core/) ---

_register(RequirementDef(
    id="core/config/config-yaml-exists",
    component=None,
    description="config.yaml exists in repo root",
    check_fn="work_buddy.health.requirement_checks.check_config_yaml_exists",
    severity="required",
    fix_hint="Create config.yaml in the work-buddy repo root. Copy from config.yaml and adjust vault_root/repos_root.",
    setup_group="repository",
))

_register(RequirementDef(
    id="core/config/config-local-exists",
    component=None,
    description="config.local.yaml exists (machine-specific overrides)",
    check_fn="work_buddy.health.requirement_checks.check_config_local_exists",
    severity="required",
    fix_hint=(
        "Create config.local.yaml from the example:\n"
        "  cp config.local.yaml.example config.local.yaml\n"
        "Then edit it with your machine-specific settings."
    ),
    setup_group="repository",
))

# vault_root is the path to the Obsidian vault — semantically owned by
# the obsidian component (without it, the bridge has nothing to read).
# Moved out of the old "bootstrap" grab-bag so the user finds it where
# they expect: under Obsidian.
_register(RequirementDef(
    id="core/config/vault-root",
    component="obsidian",
    description="vault_root points to an existing directory",
    check_fn="work_buddy.health.requirement_checks.check_vault_root",
    severity="required",
    fix_hint="Set vault_root in config.yaml to your Obsidian vault path, e.g. '/path/to/your/vault'.",
    setup_group="obsidian",
))

_register(RequirementDef(
    id="core/config/repos-root",
    component=None,
    description="repos_root points to an existing directory",
    check_fn="work_buddy.health.requirement_checks.check_repos_root",
    severity="recommended",
    fix_hint="Set repos_root in config.yaml to your git repos directory.",
    setup_group="repository",
    fix_kind="input_required",
    fix_fn="work_buddy.health.fixers.fix_repos_root",
    fix_params={
        "path": {
            "type": "path",
            "label": "Path to your repos directory",
            "hint": "Absolute path to the directory where your git repos live, e.g. C:\\repos or /home/you/code",
            "required": True,
        },
    },
    fix_preview="Validates the path exists, then sets repos_root in config.yaml.",
))

_register(RequirementDef(
    id="core/config/timezone",
    component=None,
    description="timezone is a valid IANA timezone",
    check_fn="work_buddy.health.requirement_checks.check_timezone",
    severity="required",
    fix_hint="Set timezone in config.yaml to a valid IANA timezone, e.g. 'America/New_York'.",
    setup_group="repository",
    fix_kind="input_required",
    fix_fn="work_buddy.health.fixers.fix_timezone",
    fix_params={
        "timezone": {
            "type": "str",
            "label": "IANA timezone",
            "hint": "e.g. America/Toronto, Europe/London, Asia/Tokyo",
            "required": True,
        },
    },
    fix_preview="Validates the timezone, then sets it in config.yaml.",
))

_register(RequirementDef(
    id="core/env/anthropic-api-key",
    component=None,
    description="Anthropic API key is reachable (env var or .env)",
    check_fn="work_buddy.health.requirement_checks.check_anthropic_api_key",
    severity="required",
    fix_hint=(
        "Set SUBAGENT_ANTHROPIC_API_KEY (preferred) or ANTHROPIC_API_KEY "
        "as an environment variable, or write it to the .env file at "
        "the repo root. work_buddy/llm/runner.py reads SUBAGENT first, "
        "falls back to ANTHROPIC, then scans .env. SUBAGENT is preferred "
        "in environments where ANTHROPIC_API_KEY is intentionally unset "
        "so spawned Claude Code sessions can fall back to OAuth/Claude Max."
    ),
    setup_group="credentials",
    fix_kind="input_required",
    fix_fn="work_buddy.health.fixers.fix_anthropic_api_key",
    fix_params={
        "api_key": {
            "type": "secret",
            "label": "Anthropic API key (sk-ant-...)",
            "hint": "Starts with sk-. Get one at console.anthropic.com.",
            "required": True,
            "secret": True,
        },
    },
    fix_preview="Writes SUBAGENT_ANTHROPIC_API_KEY=<your-key> to the repo .env file.",
))

_register(RequirementDef(
    id="core/data/writable",
    component=None,
    description="data/ directory exists and is writable",
    check_fn="work_buddy.health.requirement_checks.check_data_writable",
    severity="required",
    fix_hint="Ensure the data/ directory in the repo root exists and is writable.",
    setup_group="repository",
    # Smoke fix for Fix-A — proves the pipeline end-to-end on the
    # cheapest possible programmatic fixer.
    fix_kind="programmatic",
    fix_fn="work_buddy.health.fixers.fix_data_writable",
    fix_preview="Create the data/ directory if missing and verify it's writable.",
))

# --- Obsidian vault structure ---
#
# Note: there used to be an `obsidian/vault/obsidian-dir` requirement
# that checked for `.obsidian/` inside the vault root. Folded into
# `check_vault_root` (above) since vault_root's semantic meaning IS
# "path to an Obsidian vault" — and a directory without `.obsidian/`
# isn't a vault, it's just a directory. Splitting that distinction
# into two requirements made the diagnostic noisier than it needed
# to be.

_register(RequirementDef(
    id="obsidian/daily-note/plugin-enabled",
    component="obsidian",
    description="Daily Notes core plugin is enabled",
    check_fn="work_buddy.health.requirement_checks.check_daily_notes_plugin",
    severity="required",
    fix_hint="Enable the 'Daily notes' core plugin in Obsidian Settings > Core Plugins.",
    setup_group="journal",
    fix_kind="agent_handoff",
    fix_preview="Spawns a Claude Code session that walks you through enabling the Daily Notes core plugin in Obsidian.",
    fix_agent_brief=(
        "You are helping the user enable the Obsidian core 'Daily notes' "
        "plugin. This is the official one that ships with Obsidian — it "
        "lives under Settings → Core plugins (NOT Community plugins).\n\n"
        "## Steps\n\n"
        "1. Open Obsidian, click the Settings gear (bottom-left).\n"
        "2. Settings → Core plugins → toggle 'Daily notes' on.\n"
        "3. (Recommended) Settings → Daily notes → set the date format "
        "to `YYYY-MM-DD` and the new file location to your journal "
        "directory (default: `journal/`).\n"
        "4. Verify it's enabled by Reading "
        "`<vault>/.obsidian/core-plugins.json` (or "
        "`core-plugins-migration.json` on newer Obsidian) — `daily-notes` "
        "should appear in the list with value `true` (or in the array).\n"
        "5. Ask the user to refresh the dashboard Settings tab."
    ),
))

_register(RequirementDef(
    id="obsidian/daily-note/dir-exists",
    component="obsidian",
    description="Journal directory exists at configured path",
    check_fn="work_buddy.health.requirement_checks.check_journal_dir",
    severity="required",
    fix_hint="Create the journal directory in your vault (default: vault_root/journal/).",
    setup_group="journal",
    fix_kind="programmatic",
    fix_fn="work_buddy.health.fixers.fix_journal_dir",
    fix_preview="Create the configured journal/ directory inside your vault if it doesn't exist.",
))

_register(RequirementDef(
    id="obsidian/daily-note/log-section",
    component="obsidian",
    description="Today's daily note has a '# Log' section",
    check_fn="work_buddy.health.requirement_checks.check_log_section",
    severity="recommended",
    fix_hint=(
        "Add a '# Log' section header to your daily note template.\n"
        "The journal subsystem appends timestamped activity entries here."
    ),
    setup_group="journal",
    fix_kind="programmatic",
    fix_fn="work_buddy.health.fixers.fix_log_section",
    fix_preview="Append '# Log' section to today's (or last available) daily note.",
))

_register(RequirementDef(
    id="obsidian/daily-note/sign-in-section",
    component="obsidian",
    description="Today's daily note has a '# Sign-In' section",
    check_fn="work_buddy.health.requirement_checks.check_sign_in_section",
    severity="recommended",
    fix_hint=(
        "Add a '# Sign-In' section to your daily note template.\n"
        "Used by the morning routine to record sleep, energy, and mood."
    ),
    setup_group="journal",
    fix_kind="programmatic",
    fix_fn="work_buddy.health.fixers.fix_sign_in_section",
    fix_preview="Append '# Sign-In' section to today's (or last available) daily note.",
))

_register(RequirementDef(
    id="obsidian/daily-note/running-notes-section",
    component="obsidian",
    description="Today's daily note has a 'Running Notes' section",
    check_fn="work_buddy.health.requirement_checks.check_running_notes_section",
    severity="recommended",
    fix_hint=(
        "Add a '# Running Notes / Considerations' section to your daily note template.\n"
        "The backlog system processes carry-over notes from this section."
    ),
    setup_group="journal",
    fix_kind="programmatic",
    fix_fn="work_buddy.health.fixers.fix_running_notes_section",
    fix_preview="Append '# Running Notes' section to today's (or last available) daily note.",
))

_register(RequirementDef(
    id="obsidian/tasks/master-list-exists",
    component="obsidian",
    description="Master task list file exists",
    check_fn="work_buddy.health.requirement_checks.check_master_task_list",
    severity="required",
    fix_hint="Create the master task list at tasks/master-task-list.md in your vault.",
    setup_group="tasks",
    fix_kind="programmatic",
    fix_fn="work_buddy.health.fixers.fix_master_task_list",
    fix_preview="Create vault/tasks/master-task-list.md with a minimal heading + Obsidian Tasks usage hint.",
))

_register(RequirementDef(
    id="obsidian/plugins/tasks-plugin",
    component="obsidian",
    description="Obsidian Tasks plugin is installed and enabled",
    check_fn="work_buddy.health.requirement_checks.check_tasks_plugin",
    severity="required",
    fix_hint="Install and enable the 'Tasks' community plugin in Obsidian.",
    setup_group="tasks",
    fix_kind="agent_handoff",
    fix_preview="Spawns a Claude Code session that walks you through installing and enabling the Tasks plugin in Obsidian.",
    fix_agent_brief=(
        "You are helping the user install and enable the Obsidian Tasks "
        "community plugin (id: obsidian-tasks-plugin), required by "
        "work-buddy for task lifecycle management.\n\n"
        "## Steps to walk the user through\n\n"
        "1. Confirm Obsidian is open with the work-buddy vault.\n"
        "2. Settings → Community Plugins → Browse → search \"Tasks\" "
        "(by Martin Schenck, NOT Tasks BMO or other forks).\n"
        "3. Install, then enable.\n"
        "4. Verify the plugin appears in `.obsidian/community-plugins.json` "
        "(use the Read tool on `<vault>/.obsidian/community-plugins.json` "
        "to confirm `obsidian-tasks-plugin` is in the list).\n"
        "5. Once verified, ask the user to refresh the dashboard Settings "
        "tab to confirm the requirement is now green.\n\n"
        "If Community Plugins is disabled (Restricted Mode), help the "
        "user turn it off first — Settings → Community plugins → Turn on "
        "community plugins."
    ),
))

_register(RequirementDef(
    id="obsidian/plugins/work-buddy-plugin",
    component="obsidian",
    description="work-buddy Obsidian bridge plugin is installed and enabled",
    check_fn="work_buddy.health.requirement_checks.check_work_buddy_plugin",
    severity="required",
    fix_hint=(
        "Clone https://github.com/KadenMc/obsidian-work-buddy into "
        ".obsidian/plugins/ and enable 'Work Buddy' under Settings → "
        "Community Plugins. This plugin provides the HTTP bridge on port "
        "27125 that every Obsidian-backed capability relies on."
    ),
    setup_group="obsidian",
    fix_kind="agent_handoff",
    fix_preview="Spawns a Claude Code session that walks you through installing the work-buddy bridge plugin into your vault.",
    fix_agent_brief=(
        "You are helping the user install the work-buddy Obsidian bridge "
        "plugin. Without it, the HTTP bridge on port 27125 doesn't exist "
        "and every Obsidian-backed capability fails.\n\n"
        "## Steps to walk the user through\n\n"
        "1. Identify the user's vault path from work-buddy config "
        "(Read `config.yaml`, look for `vault_root`).\n"
        "2. Clone the plugin into the vault's plugins directory:\n"
        "   ```bash\n"
        "   cd <vault>/.obsidian/plugins\n"
        "   git clone https://github.com/KadenMc/obsidian-work-buddy.git\n"
        "   cd obsidian-work-buddy\n"
        "   npm install\n"
        "   npm run build\n"
        "   ```\n"
        "3. Open Obsidian → Settings → Community Plugins → enable \"Work Buddy\".\n"
        "4. Confirm `work-buddy` appears in `.obsidian/community-plugins.json`.\n"
        "5. Verify the bridge responds: `curl http://127.0.0.1:27125/health`.\n"
        "6. Ask the user to refresh the dashboard Settings tab.\n\n"
        "Note: this plugin needs Node.js installed for the build. If the "
        "user doesn't have it, point them at https://nodejs.org first."
    ),
))

_register(RequirementDef(
    id="obsidian/contracts/dir-exists",
    component="obsidian",
    description="Contracts directory exists in vault",
    check_fn="work_buddy.health.requirement_checks.check_contracts_dir",
    severity="recommended",
    fix_hint="Create the contracts directory in your vault (default: work-buddy/contracts/).",
    setup_group="contracts",
    fix_kind="programmatic",
    fix_fn="work_buddy.health.fixers.fix_contracts_dir",
    fix_preview="Create the configured contracts directory inside your vault.",
))

_register(RequirementDef(
    id="obsidian/knowledge/personal-path",
    component="obsidian",
    description="Personal knowledge vault path exists",
    check_fn="work_buddy.health.requirement_checks.check_personal_knowledge_path",
    severity="recommended",
    fix_hint="Create the personal knowledge directory in your vault (default: Meta/WorkBuddy/).",
    setup_group="knowledge",
    fix_kind="programmatic",
    fix_fn="work_buddy.health.fixers.fix_personal_knowledge_dir",
    fix_preview="Create the configured personal-knowledge directory inside your vault.",
))

# --- Integrations ---

_register(RequirementDef(
    id="integrations/hindsight/pg-scheduled-task",
    component="hindsight",
    description=(
        "Scheduled task for PostgreSQL auto-start exists"
        if _IS_WINDOWS else "PostgreSQL auto-start is configured"
    ),
    check_fn="work_buddy.health.requirement_checks.check_pg_scheduled_task",
    severity="recommended",
    fix_hint=(
        "Create a Windows scheduled task named 'Hindsight-PostgreSQL' to start\n"
        "PostgreSQL on login. See scripts/start-hindsight.sh for the ordering."
        if _IS_WINDOWS else
        "Configure PostgreSQL to start on login via systemd user unit or shell profile.\n"
        "See scripts/start-hindsight.sh for the startup ordering."
    ),
    setup_group="memory",
    fix_kind="agent_handoff",
    fix_preview=(
        "Spawns a Claude Code session to set up PostgreSQL auto-start "
        "(Hindsight depends on PostgreSQL being available)."
    ),
    fix_agent_brief=(
        "You are helping the user set up PostgreSQL to auto-start so that "
        "Hindsight (the personal-memory layer) can boot reliably.\n\n"
        "## Context\n"
        "Hindsight requires PostgreSQL on port 5432. The user's machine "
        "is "
        + ("Windows — set up a Scheduled Task named 'Hindsight-PostgreSQL'."
           if _IS_WINDOWS else
           "non-Windows — set up a systemd user unit or shell-profile entry.")
        + "\n\n"
        "## Steps\n\n"
        "1. Find the user's PostgreSQL data directory and binary path "
        "(`pg_ctl`).\n"
        "2. Read `scripts/start-hindsight.sh` in the work-buddy repo for "
        "the exact start ordering Hindsight expects.\n"
        + ("3. On Windows: use Task Scheduler (or PowerShell "
           "`Register-ScheduledTask`) to create a task named "
           "'Hindsight-PostgreSQL' that runs `pg_ctl start -D <data-dir>` "
           "at user logon.\n"
           if _IS_WINDOWS else
           "3. On Linux/macOS: create a systemd user unit "
           "(`~/.config/systemd/user/hindsight-postgres.service`) or add "
           "a startup hook to the user's shell profile that runs "
           "`pg_ctl -D <data-dir> -l <log> start`.\n")
        + "4. Verify by running the scheduled task / systemd unit once "
        "manually and confirming PostgreSQL is listening on port 5432.\n"
        "5. Ask the user to refresh the dashboard Settings tab.\n\n"
        "If PostgreSQL itself isn't installed yet, point the user at the "
        "Hindsight setup docs first."
    ),
))

_register(RequirementDef(
    id="integrations/chrome/native-host",
    component="chrome_extension",
    description="Chrome native messaging host manifest is registered",
    check_fn="work_buddy.health.requirement_checks.check_chrome_native_host",
    severity="required",
    fix_hint=(
        "Register the Chrome native messaging host:\n"
        "  cd work_buddy/chrome_native_host && python install.py\n"
        + ("Manifest location: %APPDATA%\\Google\\Chrome\\NativeMessagingHosts\\" if _IS_WINDOWS
           else "Manifest location: ~/.config/google-chrome/NativeMessagingHosts/" if not platform.system() == "Darwin"
           else "Manifest location: ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/")
        + "\nSee chrome_native_host/README.md for details."
    ),
    setup_group="chrome",
    fix_kind="agent_handoff",
    fix_preview=(
        "Spawns a Claude Code session to register the Chrome native "
        "messaging host so the browser extension can talk to work-buddy."
    ),
    fix_agent_brief=(
        "You are helping the user register the Chrome native messaging "
        "host for the work-buddy browser extension. Without this, Chrome "
        "can't send tab snapshots or receive close/group/focus commands "
        "from work-buddy.\n\n"
        "## Steps\n\n"
        "1. Read `work_buddy/chrome_native_host/README.md` in the repo "
        "for the latest install instructions and the manifest schema.\n"
        "2. Run the installer: `cd work_buddy/chrome_native_host && "
        "python install.py`. This writes a manifest pointing at the "
        "Python launcher into Chrome's NativeMessagingHosts directory.\n"
        "3. Manifest location depends on OS:\n"
        + ("   - Windows: `%APPDATA%\\Google\\Chrome\\NativeMessagingHosts\\`\n"
           if _IS_WINDOWS else
           "   - macOS: `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/`\n"
           "   - Linux: `~/.config/google-chrome/NativeMessagingHosts/`\n")
        + "4. Confirm the work-buddy Chrome extension is installed and "
        "enabled in chrome://extensions/.\n"
        "5. Reload the extension and verify a tab snapshot lands in "
        "`data/chrome/ledger.json` (the dashboard's Chrome status will "
        "go green within ~120 s).\n"
        "6. Ask the user to refresh the dashboard Settings tab."
    ),
))

# --- Services ---

_register(RequirementDef(
    id="services/telegram/bot-token",
    component="telegram",
    description="Telegram bot token is configured",
    check_fn="work_buddy.health.requirement_checks.check_telegram_bot_token",
    severity="required",
    fix_hint=(
        "Set TELEGRAM_BOT_TOKEN in your .env file. Create a bot via @BotFather\n"
        "on Telegram and copy the token."
    ),
    setup_group="telegram",
    fix_kind="input_required",
    fix_fn="work_buddy.health.fixers.fix_telegram_bot_token",
    fix_params={
        "bot_token": {
            "type": "secret",
            "label": "Telegram bot token",
            "hint": "Format: 123456789:AA…  (from @BotFather on Telegram)",
            "required": True,
            "secret": True,
        },
    },
    fix_preview="Writes TELEGRAM_BOT_TOKEN=<your-token> to the repo .env file.",
))


# ---------------------------------------------------------------------------
# RequirementChecker
# ---------------------------------------------------------------------------

class RequirementChecker:
    """Validates requirements against the current environment."""

    def _run_check(self, req: RequirementDef) -> RequirementResult:
        """Execute a single requirement check."""
        try:
            module_path, fn_name = req.check_fn.rsplit(".", 1)
            module = importlib.import_module(module_path)
            check_fn = getattr(module, fn_name)
            result = check_fn()
            return RequirementResult(
                id=req.id,
                ok=result.get("ok", False),
                detail=result.get("detail", ""),
                fix_hint=req.fix_hint,
                severity=req.severity,
                component=req.component,
            )
        except Exception as exc:
            log.warning("Requirement check %s failed: %s", req.id, exc)
            return RequirementResult(
                id=req.id,
                ok=False,
                detail=f"Check raised an error: {exc}",
                fix_hint=req.fix_hint,
                severity=req.severity,
                component=req.component,
            )

    def check_all(self, include_unwanted: bool = False) -> list[RequirementResult]:
        """Validate all requirements, optionally filtering by user preferences.

        If ``include_unwanted`` is False (default), skips requirements for
        components the user has explicitly opted out of (``wanted: false``).
        Core requirements (``component=None``) always run.
        """
        from work_buddy.health.preferences import is_wanted

        results = []
        for req in REQUIREMENT_REGISTRY.values():
            if req.component is not None and not include_unwanted:
                pref = is_wanted(req.component)
                if pref is False:
                    continue
            results.append(self._run_check(req))
        return results

    def check_bootstrap(self) -> list[RequirementResult]:
        """Check only core/* requirements (fast, no component filter)."""
        return [
            self._run_check(req)
            for req in REQUIREMENT_REGISTRY.values()
            if req.id.startswith("core/")
        ]

    def check_component(self, component_id: str) -> list[RequirementResult]:
        """Check requirements for a specific component."""
        return [
            self._run_check(req)
            for req in REQUIREMENT_REGISTRY.values()
            if req.component == component_id
        ]

    def check_group(self, group_name: str) -> list[RequirementResult]:
        """Check requirements for a setup group (e.g. 'journal', 'tasks')."""
        return [
            self._run_check(req)
            for req in REQUIREMENT_REGISTRY.values()
            if req.setup_group == group_name
        ]

    @staticmethod
    def summarize(results: list[RequirementResult]) -> dict[str, Any]:
        """Produce a summary from a list of results."""
        passed = sum(1 for r in results if r.ok)
        failed_required = [r for r in results if not r.ok and r.severity == "required"]
        failed_recommended = [r for r in results if not r.ok and r.severity == "recommended"]
        return {
            "total": len(results),
            "passed": passed,
            "failed_required": len(failed_required),
            "failed_recommended": len(failed_recommended),
            "all_required_pass": len(failed_required) == 0,
            "failures": [r.to_dict() for r in results if not r.ok],
        }
