"""Component definitions and catalog for the health subsystem.

Each ComponentDef describes:
- Identity: id, display_name, category
- Dependencies: which other components must be healthy first
- Health source: where to read status (tool_probe, sidecar, composite, custom)
- Troubleshooting: ordered check sequence with fix instructions

Component definitions are Python data (not YAML), co-located with
troubleshooting knowledge — following the proven ToolProbe pattern
from tools.py.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field

# Detect once at import time — used to select platform-specific fix hints.
_IS_WINDOWS = platform.system() == "Windows"
_IS_MAC = platform.system() == "Darwin"


@dataclass
class CheckStep:
    """A single step in a diagnostic check sequence.

    Attributes:
        description: What this step checks, e.g. "PostgreSQL accepting connections".
        check_fn: Dotted import path to a callable returning ``{ok: bool, detail: str}``.
        on_fail: Human-readable fix instructions when this step fails.
    """

    description: str
    check_fn: str
    on_fail: str


@dataclass
class ComponentDef:
    """Definition of a health-monitored component.

    Attributes:
        id: Short identifier matching tool probe IDs where applicable.
        display_name: Human-readable name.
        category: One of "external", "integration", "service", "plugin".
        depends_on: Component IDs that must be healthy first.
        health_source: How to determine status:
            - "tool_probe": reads from tool_status.json (application-level)
            - "sidecar": reads from sidecar_state.json (process-level)
            - "composite": merges tool_probe + sidecar
            - "custom": uses only check_sequence
        check_sequence: Ordered diagnostic steps run by DiagnosticRunner.
        sidecar_service: Sidecar service name (if health_source involves sidecar).
        is_core: True means the user cannot opt out. Core components are
            always treated as wanted by the preference loader and the UI
            hides their toggle. Use for prerequisites without which
            work-buddy itself can't function (sidecar, dashboard, and
            the services the dashboard hard-depends on).
    """

    id: str
    display_name: str
    category: str
    # Hard deps: if the target is unhealthy, THIS component is `blocked`.
    # Use only for targets without which this component literally cannot
    # function (e.g. Hindsight → PostgreSQL).
    depends_on: list[str] = field(default_factory=list)
    # Soft deps: if the target is unhealthy, this component is at most
    # `degraded`. Use for optional helpers whose absence reduces
    # functionality but does not break the component (e.g. dashboard →
    # embedding service — dashboard falls back to substring search).
    # Added in the hard/soft-deps refactor (2026-04-22).
    soft_depends_on: list[str] = field(default_factory=list)
    # Per-soft-dep notes describing what specifically happens when the
    # target is unavailable. Keyed by the component id (matches an
    # entry in soft_depends_on). Propagated onto the corresponding
    # Edge's fallback_note and surfaced in the Settings UI. Use these
    # to distinguish "graceful fallback" from "this specific feature
    # just disappears" — both qualify as "soft" but the user experience
    # is very different.
    soft_dep_notes: dict[str, str] = field(default_factory=dict)
    health_source: str = "tool_probe"
    check_sequence: list[CheckStep] = field(default_factory=list)
    sidecar_service: str | None = None
    requirements: list[str] = field(default_factory=list)  # Requirement IDs from REQUIREMENT_REGISTRY
    is_core: bool = False


# ---------------------------------------------------------------------------
# Component catalog
# ---------------------------------------------------------------------------

COMPONENT_CATALOG: dict[str, ComponentDef] = {}


def _register(comp: ComponentDef) -> None:
    COMPONENT_CATALOG[comp.id] = comp


# --- External dependencies ---

_register(ComponentDef(
    id="sidecar",
    display_name="Sidecar Daemon",
    category="external",
    is_core=True,  # nothing works without the sidecar
    health_source="sidecar",
    sidecar_service="sidecar",  # synthetic entry synthesized by HealthEngine._load
    check_sequence=[
        CheckStep(
            description="Sidecar daemon process alive and ticking",
            check_fn="work_buddy.health.checks.check_sidecar_heartbeat",
            on_fail=(
                "The work-buddy sidecar daemon is not running (or has "
                "stopped heartbeating). Start it with:\n"
                + ("  Start-ScheduledTask 'WB-Sidecar'" if _IS_WINDOWS
                   else "  python -m work_buddy.sidecar &")
                + "\n\nMost work-buddy capabilities depend on the sidecar — "
                "if it's down, health checks, scheduled jobs, and inter-agent "
                "messaging will be unavailable."
            ),
        ),
    ],
))

_register(ComponentDef(
    id="postgresql",
    display_name="PostgreSQL",
    category="external",
    health_source="tool_probe",
    check_sequence=[
        CheckStep(
            description="PostgreSQL accepting connections on port 5432",
            check_fn="work_buddy.health.checks.check_postgresql",
            on_fail=(
                "PostgreSQL is not running on port 5432. Start it:\n"
                + ("  Start-ScheduledTask 'Hindsight-PostgreSQL'" if _IS_WINDOWS
                   else "  systemctl --user start hindsight-postgres")
                + "\n  Or manually: pg_ctl -D <data_dir> -l <logfile> start"
            ),
        ),
    ],
))

_register(ComponentDef(
    id="lmstudio",
    display_name="LM Studio",
    category="external",
    # Optional. The embedding subsystem falls back to sentence-transformers
    # when LM Studio isn't reachable, and nothing in work-buddy hard-
    # requires LM Studio — LLM calls also have Anthropic fallbacks. So
    # users can opt out entirely via Settings and the wizard hides it.
    is_core=False,
    # tool_probe (not "custom"): an always-on external service, polled
    # on the normal registry-build cadence via
    # ``work_buddy.tools._probe_lmstudio``. "custom" is reserved for
    # components that only resolve when the user explicitly clicks
    # Diagnose; using it here would pin the dashboard at "unknown"
    # indefinitely.
    health_source="tool_probe",
    # Listed first so the diagnostic panel leads with the reachable check.
    requirements=["services/lmstudio/reachable"],
    check_sequence=[
        CheckStep(
            description=(
                "LM Studio reachable on configured base URL (/v1/models)"
            ),
            check_fn="work_buddy.health.checks.check_lmstudio",
            on_fail=(
                "LM Studio is not reachable. Open LM Studio and start "
                "its local server (Developer tab → Start Server). The "
                "default URL is http://localhost:1234 — override via "
                "lmstudio.base_url in config.yaml if you run it on a "
                "different host or port.\n\n"
                "LM Studio is optional — it's only needed if you've "
                "opted into offloading an embedding model to it "
                "(embedding.models.<key>.provider: lmstudio). The "
                "passage-encoder offload procedure, including GGUF "
                "audit and drift-test steps, is documented in "
                "docs/handbook/features_lmstudio-offload-setup.md."
            ),
        ),
    ],
))

# --- Core integrations ---

_register(ComponentDef(
    id="obsidian",
    display_name="Obsidian Bridge",
    category="integration",
    health_source="tool_probe",
    requirements=[
        # Foundational: where the vault lives. check_vault_root verifies
        # both that the path exists AND that it contains an .obsidian/
        # subdirectory (= it's actually an Obsidian vault), so we don't
        # need a separate `obsidian/vault/obsidian-dir` requirement.
        "core/config/vault-root",
        # The bridge plugin is the reason this component exists — without
        # it the HTTP probe has nothing to answer it. Listed first so
        # it's the first thing users see when obsidian is broken.
        "obsidian/plugins/work-buddy-plugin",
        "obsidian/daily-note/plugin-enabled",
        "obsidian/daily-note/dir-exists",
        "obsidian/daily-note/log-section",
        "obsidian/daily-note/sign-in-section",
        "obsidian/daily-note/running-notes-section",
        "obsidian/tasks/master-list-exists",
        "obsidian/plugins/tasks-plugin",
        "obsidian/contracts/dir-exists",
        "obsidian/knowledge/personal-path",
    ],
    check_sequence=[
        CheckStep(
            description="Obsidian bridge HTTP health endpoint",
            check_fn="work_buddy.health.checks.check_obsidian_bridge",
            on_fail=(
                "Obsidian is not running, or the work-buddy bridge "
                "plugin is not active. Open Obsidian and verify the plugin "
                "is enabled in Settings > Community Plugins."
            ),
        ),
    ],
))

_register(ComponentDef(
    id="tailscale",
    display_name="Tailscale Remote Access",
    category="integration",
    # Non-core: a local-only setup is a legitimate configuration. Users
    # who don't want remote dashboard access opt out via preferences and
    # the requirement / probe pair below skips entirely.
    is_core=False,
    # 'custom' over 'tool_probe' — we don't want the engine polling
    # `tailscale status` continuously. Status resolves only when the user
    # asks (setup_help / setup_wizard diagnose).
    health_source="custom",
    requirements=[
        "integrations/tailscale/installed",
        "integrations/tailscale/serve-configured",
    ],
    check_sequence=[
        CheckStep(
            description="Tailscale daemon running",
            check_fn="work_buddy.health.checks.check_tailscale_daemon",
            on_fail=(
                "Tailscale daemon is not running. On Windows, open the "
                "Tailscale tray app, or run `net start Tailscale` from an "
                "elevated PowerShell. On macOS / Linux, launch the app "
                "or `sudo tailscale up`."
            ),
        ),
        CheckStep(
            description="This device online on the tailnet",
            check_fn="work_buddy.health.checks.check_tailscale_self_online",
            on_fail=(
                "This device is not currently connected to the tailnet. "
                "Open the Tailscale app and toggle it on / sign in. Node "
                "keys can also expire after long inactivity — "
                "reauthenticate at "
                "https://login.tailscale.com/admin/machines if prompted."
            ),
        ),
    ],
))

_register(ComponentDef(
    id="hindsight",
    display_name="Hindsight Memory Server",
    category="integration",
    depends_on=["postgresql"],
    health_source="composite",
    sidecar_service=None,  # not sidecar-managed, but has tool probe
    requirements=["integrations/hindsight/pg-scheduled-task"],
    check_sequence=[
        CheckStep(
            description="PostgreSQL is running (dependency)",
            check_fn="work_buddy.health.checks.check_postgresql",
            on_fail=(
                "Hindsight requires PostgreSQL. Start it first:\n"
                + ("  Start-ScheduledTask 'Hindsight-PostgreSQL'" if _IS_WINDOWS
                   else "  systemctl --user start hindsight-postgres")
                + "\n  Or manually: pg_ctl -D <data_dir> -l <logfile> start"
            ),
        ),
        CheckStep(
            description="Hindsight API responding on port 8888",
            check_fn="work_buddy.health.checks.check_hindsight_api",
            on_fail=(
                "Hindsight API is not responding. This can happen when:\n"
                "  1. PostgreSQL was not running when Hindsight started\n"
                "  2. The API crashed but the async worker survived (half-dead state)\n"
                "  3. The terminal window was accidentally closed\n"
                "Fix:\n"
                + ("  1. Kill remaining processes: Get-Process *hindsight* | Stop-Process\n"
                   "  2. Ensure PostgreSQL is running (check port 5432)\n"
                   "  3. Restart: Start-ScheduledTask 'Hindsight-API'\n"
                   "     Or manually: conda activate work-buddy && hindsight-api"
                   if _IS_WINDOWS else
                   "  1. Kill remaining processes: pkill -f hindsight\n"
                   "  2. Ensure PostgreSQL is running (check port 5432)\n"
                   "  3. Restart: conda activate work-buddy && hindsight-api &")
                + "\nSee also: scripts/start-hindsight.sh, SETUP.md"
            ),
        ),
    ],
))

_register(ComponentDef(
    id="thunderbird",
    display_name="Thunderbird Email Bridge",
    category="integration",
    health_source="tool_probe",
    requirements=["integrations/thunderbird/bridge"],
    check_sequence=[
        CheckStep(
            description=(
                "thunderbird-work-buddy companion add-on responding on the "
                "discovered localhost port"
            ),
            check_fn="work_buddy.health.checks.check_thunderbird_bridge",
            on_fail=(
                "The thunderbird-work-buddy bridge is not reachable. "
                "Common causes:\n"
                "  1. Thunderbird is closed — open it and re-probe.\n"
                "  2. The companion add-on isn't installed in the active "
                "Thunderbird profile. Install via "
                "'Add-ons and Themes → ⚙ → Install Add-on From File' with "
                "the freshly built dist/thunderbird-work-buddy.xpi.\n"
                "  3. The add-on is installed but no accounts are ticked "
                "in its options page (default-deny). Open the options "
                "and tick the account(s) you want exposed.\n"
                "  4. Stale connection file from a prior Thunderbird "
                "session. Restart Thunderbird to refresh the per-startup "
                "auth token.\n"
                "  5. The integration is opted out via "
                "tools.thunderbird.enabled: false — flip to true in "
                "config.local.yaml after installing the add-on."
            ),
        ),
    ],
))

_register(ComponentDef(
    id="chrome_extension",
    display_name="Chrome Tab Extension",
    category="integration",
    health_source="tool_probe",
    requirements=["integrations/chrome/native-host"],
    check_sequence=[
        CheckStep(
            description="Chrome tab export file exists and is fresh (<120s)",
            check_fn="work_buddy.health.checks.check_chrome_ledger",
            on_fail=(
                "Chrome extension is not exporting tabs. Verify:\n"
                "  1. Chrome is running\n"
                "  2. The work-buddy tab exporter extension is installed and enabled\n"
                "  3. The extension's vault path matches your Obsidian vault"
            ),
        ),
    ],
))

# --- Sidecar-managed services ---

_SIDECAR_LOG_HINT = (
    "Check sidecar logs: <data_root>/logs/sidecar.log\n"
    "Restart sidecar: "
    + ("Start-ScheduledTask 'WB-Sidecar'" if _IS_WINDOWS
       else "python -m work_buddy.sidecar &")
)

_register(ComponentDef(
    id="messaging",
    display_name="Messaging Service",
    category="service",
    is_core=True,  # inter-agent + session hooks depend on it
    depends_on=["sidecar"],  # supervised by the sidecar daemon
    health_source="composite",
    sidecar_service="messaging",
    check_sequence=[
        CheckStep(
            description="Messaging service health endpoint (port 5123)",
            check_fn="work_buddy.health.checks.check_sidecar_service_messaging",
            on_fail=f"Messaging service is not running.\n{_SIDECAR_LOG_HINT}",
        ),
    ],
))

_register(ComponentDef(
    id="embedding",
    display_name="Embedding Service",
    category="service",
    is_core=True,  # hybrid search + knowledge-index dense vectors depend on it
    depends_on=["sidecar"],  # supervised by the sidecar daemon
    # Optional offload of the passage-side document encoder to LM
    # Studio. When a user configures
    # ``embedding.models.<key>.provider: lmstudio`` AND LM Studio is
    # reachable, bulk document encoding runs remotely (memory win —
    # ~500 MB RSS stays off the main machine). When LM Studio is down
    # (or the user never opted in), the sentence-transformers fallback
    # handles everything. The component itself keeps working either
    # way, so this is modeled as soft.
    soft_depends_on=["lmstudio"],
    soft_dep_notes={
        "lmstudio": (
            "Document-side passage encoding falls back to the local "
            "sentence-transformers model when LM Studio is unreachable "
            "(only applies if you've opted into "
            "embedding.models.<key>.provider: lmstudio in config — "
            "otherwise this dep is ignored). Query encoding never "
            "offloads and is unaffected."
        ),
    },
    health_source="composite",
    sidecar_service="embedding",
    check_sequence=[
        CheckStep(
            description="Embedding service health endpoint (port 5124)",
            check_fn="work_buddy.health.checks.check_sidecar_service_embedding",
            on_fail=f"Embedding service is not running.\n{_SIDECAR_LOG_HINT}",
        ),
    ],
))

_register(ComponentDef(
    id="telegram",
    display_name="Telegram Bot",
    category="service",
    depends_on=["sidecar"],  # supervised by the sidecar daemon
    health_source="composite",
    sidecar_service="telegram",
    requirements=["services/telegram/bot-token"],
    check_sequence=[
        CheckStep(
            description="Telegram bot service health endpoint (port 5125)",
            check_fn="work_buddy.health.checks.check_sidecar_service_telegram",
            on_fail=(
                "Telegram bot is not running. Verify:\n"
                "  1. TELEGRAM_BOT_TOKEN is set in .env\n"
                "  2. telegram.enabled: true in config.yaml\n"
                "  3. sidecar.services.telegram.enabled: true in config.yaml"
            ),
        ),
    ],
))

_register(ComponentDef(
    id="dashboard",
    display_name="Dashboard",
    category="service",
    is_core=True,  # this is the UI; turning it off turns off the Settings page itself
    # The sidecar daemon supervises the dashboard process — if the
    # sidecar is down the dashboard won't restart when it crashes, and
    # there's no coordinated way to keep it alive. Modeled as hard.
    depends_on=["sidecar"],
    # Soft helpers — the dashboard *itself* keeps running without them,
    # but specific features change state. Each note below describes
    # precisely what the user loses, so the UI doesn't lie by saying
    # "may be reduced" when the truth is "this feature is gone."
    soft_depends_on=["embedding", "messaging", "obsidian", "hindsight"],
    soft_dep_notes={
        "embedding": (
            "Hybrid search on tasks/palette falls back to substring "
            "matching. Chat-content and commit IR search endpoints "
            "return empty or error — there is no substring fallback "
            "for those."
        ),
        "messaging": (
            "Acknowledge-poller stops: cross-surface notification "
            "dismissal (click to dismiss in Obsidian, have it vanish "
            "from the dashboard) will no longer work until messaging "
            "is healthy again."
        ),
        "obsidian": (
            "Task/contract/journal panels still read the markdown "
            "files directly, so basic reads work; but any feature "
            "that routes through the bridge (live task mutations, "
            "Obsidian command-palette execution, vault writes) is "
            "unavailable."
        ),
        "hindsight": (
            "Project-detail panel shows an empty memory section; "
            "project-memory recall is unavailable. The rest of the "
            "projects view works."
        ),
    },
    health_source="sidecar",
    sidecar_service="dashboard",
    check_sequence=[
        CheckStep(
            description="Dashboard service health endpoint (port 5127)",
            check_fn="work_buddy.health.checks.check_sidecar_service_dashboard",
            on_fail=f"Dashboard service is not running.\n{_SIDECAR_LOG_HINT}",
        ),
    ],
))

# --- Obsidian plugins (depend on obsidian) ---

_register(ComponentDef(
    id="smart_connections",
    display_name="Smart Connections",
    category="plugin",
    depends_on=["obsidian"],
    health_source="tool_probe",
    check_sequence=[
        CheckStep(
            description="Smart Connections plugin loaded in Obsidian",
            check_fn="work_buddy.health.checks.check_obsidian_plugin_smart",
            on_fail="Smart Connections plugin is not active in Obsidian.",
        ),
    ],
))

_register(ComponentDef(
    id="datacore",
    display_name="Datacore Plugin",
    category="plugin",
    depends_on=["obsidian"],
    health_source="tool_probe",
    check_sequence=[
        CheckStep(
            description="Datacore plugin loaded in Obsidian",
            check_fn="work_buddy.health.checks.check_obsidian_plugin_datacore",
            on_fail="Datacore plugin is not active in Obsidian.",
        ),
    ],
))

_register(ComponentDef(
    id="google_calendar",
    display_name="Google Calendar Plugin",
    category="plugin",
    depends_on=["obsidian"],
    health_source="tool_probe",
    check_sequence=[
        CheckStep(
            description="Google Calendar plugin loaded in Obsidian",
            check_fn="work_buddy.health.checks.check_obsidian_plugin_calendar",
            on_fail="Google Calendar plugin is not active in Obsidian.",
        ),
    ],
))

_register(ComponentDef(
    id="github_backups",
    display_name="GitHub Releases Backup",
    category="integration",
    # Optional: users who don't want off-machine backups skip the
    # requirements entirely via the preference toggle. Local rolling
    # backups run unconditionally (regardless of this component's
    # wanted state); the component gates only the remote push to
    # GitHub Releases.
    is_core=False,
    # 'custom' over 'tool_probe' — the freshness check reads a local
    # JSON file (.data/backups/last_run.json) written by the sidecar
    # cron, never hits GitHub on the hot path. We don't want the
    # control graph hammering the GitHub API on every refresh.
    health_source="custom",
    requirements=[
        "integrations/github_backups/gh-cli-installed",
        "integrations/github_backups/gh-authenticated",
        "integrations/github_backups/repo-configured",
    ],
    check_sequence=[
        CheckStep(
            description="Last backup succeeded and is within cadence window",
            check_fn="work_buddy.health.checks.check_github_backup_freshness",
            on_fail=(
                "The last GitHub backup either failed or is overdue. "
                "Inspect .data/backups/last_run.json for the error detail, "
                "or run /wb-backup-now to push a snapshot immediately."
            ),
        ),
    ],
))
