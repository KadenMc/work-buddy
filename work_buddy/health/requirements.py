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
        fix_hint: Human-readable fix instructions.
        setup_group: Wizard grouping: ``"bootstrap"``, ``"journal"``, ``"tasks"``, etc.
    """

    id: str
    component: str | None
    description: str
    check_fn: str
    severity: str  # "required" | "recommended"
    fix_hint: str
    setup_group: str


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
    setup_group="bootstrap",
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
    setup_group="bootstrap",
))

_register(RequirementDef(
    id="core/config/vault-root",
    component=None,
    description="vault_root points to an existing directory",
    check_fn="work_buddy.health.requirement_checks.check_vault_root",
    severity="required",
    fix_hint="Set vault_root in config.yaml to your Obsidian vault path, e.g. '/path/to/your/vault'.",
    setup_group="bootstrap",
))

_register(RequirementDef(
    id="core/config/repos-root",
    component=None,
    description="repos_root points to an existing directory",
    check_fn="work_buddy.health.requirement_checks.check_repos_root",
    severity="recommended",
    fix_hint="Set repos_root in config.yaml to your git repos directory.",
    setup_group="bootstrap",
))

_register(RequirementDef(
    id="core/config/timezone",
    component=None,
    description="timezone is a valid IANA timezone",
    check_fn="work_buddy.health.requirement_checks.check_timezone",
    severity="required",
    fix_hint="Set timezone in config.yaml to a valid IANA timezone, e.g. 'America/New_York'.",
    setup_group="bootstrap",
))

_register(RequirementDef(
    id="core/env/anthropic-api-key",
    component=None,
    description="ANTHROPIC_API_KEY environment variable is set",
    check_fn="work_buddy.health.requirement_checks.check_anthropic_api_key",
    severity="required",
    fix_hint="Set the ANTHROPIC_API_KEY environment variable with your Anthropic API key.",
    setup_group="bootstrap",
))

_register(RequirementDef(
    id="core/data/writable",
    component=None,
    description="data/ directory exists and is writable",
    check_fn="work_buddy.health.requirement_checks.check_data_writable",
    severity="required",
    fix_hint="Ensure the data/ directory in the repo root exists and is writable.",
    setup_group="bootstrap",
))

# --- Obsidian vault structure ---

_register(RequirementDef(
    id="obsidian/vault/obsidian-dir",
    component="obsidian",
    description=".obsidian/ directory exists in vault root",
    check_fn="work_buddy.health.requirement_checks.check_obsidian_dir",
    severity="required",
    fix_hint="Open the vault in Obsidian at least once to create the .obsidian/ directory.",
    setup_group="vault",
))

_register(RequirementDef(
    id="obsidian/daily-note/plugin-enabled",
    component="obsidian",
    description="Daily Notes core plugin is enabled",
    check_fn="work_buddy.health.requirement_checks.check_daily_notes_plugin",
    severity="required",
    fix_hint="Enable the 'Daily notes' core plugin in Obsidian Settings > Core Plugins.",
    setup_group="journal",
))

_register(RequirementDef(
    id="obsidian/daily-note/dir-exists",
    component="obsidian",
    description="Journal directory exists at configured path",
    check_fn="work_buddy.health.requirement_checks.check_journal_dir",
    severity="required",
    fix_hint="Create the journal directory in your vault (default: vault_root/journal/).",
    setup_group="journal",
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
))

_register(RequirementDef(
    id="obsidian/tasks/master-list-exists",
    component="obsidian",
    description="Master task list file exists",
    check_fn="work_buddy.health.requirement_checks.check_master_task_list",
    severity="required",
    fix_hint="Create the master task list at tasks/master-task-list.md in your vault.",
    setup_group="tasks",
))

_register(RequirementDef(
    id="obsidian/plugins/tasks-plugin",
    component="obsidian",
    description="Obsidian Tasks plugin is installed and enabled",
    check_fn="work_buddy.health.requirement_checks.check_tasks_plugin",
    severity="required",
    fix_hint="Install and enable the 'Tasks' community plugin in Obsidian.",
    setup_group="tasks",
))

_register(RequirementDef(
    id="obsidian/contracts/dir-exists",
    component="obsidian",
    description="Contracts directory exists in vault",
    check_fn="work_buddy.health.requirement_checks.check_contracts_dir",
    severity="recommended",
    fix_hint="Create the contracts directory in your vault (default: work-buddy/contracts/).",
    setup_group="contracts",
))

_register(RequirementDef(
    id="obsidian/knowledge/personal-path",
    component="obsidian",
    description="Personal knowledge vault path exists",
    check_fn="work_buddy.health.requirement_checks.check_personal_knowledge_path",
    severity="recommended",
    fix_hint="Create the personal knowledge directory in your vault (default: Meta/WorkBuddy/).",
    setup_group="knowledge",
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
