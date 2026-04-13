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

from dataclasses import dataclass, field


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
    """

    id: str
    display_name: str
    category: str
    depends_on: list[str] = field(default_factory=list)
    health_source: str = "tool_probe"
    check_sequence: list[CheckStep] = field(default_factory=list)
    sidecar_service: str | None = None


# ---------------------------------------------------------------------------
# Component catalog
# ---------------------------------------------------------------------------

COMPONENT_CATALOG: dict[str, ComponentDef] = {}


def _register(comp: ComponentDef) -> None:
    COMPONENT_CATALOG[comp.id] = comp


# --- External dependencies ---

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
                "PostgreSQL is not running. Start it with:\n"
                "  - pg_ctl start -D <data_dir>\n"
                "  - Windows: Start-ScheduledTask 'Hindsight-PostgreSQL'\n"
                "  - Linux: systemctl --user start hindsight-postgres"
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
    check_sequence=[
        CheckStep(
            description="Obsidian bridge HTTP health endpoint",
            check_fn="work_buddy.health.checks.check_obsidian_bridge",
            on_fail=(
                "Obsidian is not running, or the obsidian-work-buddy bridge "
                "plugin is not active. Open Obsidian and verify the plugin "
                "is enabled in Settings > Community Plugins."
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
    check_sequence=[
        CheckStep(
            description="PostgreSQL is running (dependency)",
            check_fn="work_buddy.health.checks.check_postgresql",
            on_fail="Hindsight requires PostgreSQL. Start PostgreSQL first.",
        ),
        CheckStep(
            description="Hindsight API responding on port 8888",
            check_fn="work_buddy.health.checks.check_hindsight_api",
            on_fail=(
                "Hindsight API is not responding. This can happen when:\n"
                "  1. PostgreSQL was not running when Hindsight started\n"
                "  2. The API crashed but the async worker survived (half-dead state)\n"
                "Fix: Kill any remaining Hindsight processes, ensure PostgreSQL "
                "is running, then restart Hindsight via scripts/start-hindsight.sh"
            ),
        ),
    ],
))

_register(ComponentDef(
    id="chrome_extension",
    display_name="Chrome Tab Extension",
    category="integration",
    health_source="tool_probe",
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

_register(ComponentDef(
    id="messaging",
    display_name="Messaging Service",
    category="service",
    health_source="composite",
    sidecar_service="messaging",
    check_sequence=[
        CheckStep(
            description="Messaging service health endpoint (port 5123)",
            check_fn="work_buddy.health.checks.check_sidecar_service_messaging",
            on_fail="Messaging service is not running. Check sidecar logs.",
        ),
    ],
))

_register(ComponentDef(
    id="embedding",
    display_name="Embedding Service",
    category="service",
    health_source="composite",
    sidecar_service="embedding",
    check_sequence=[
        CheckStep(
            description="Embedding service health endpoint (port 5124)",
            check_fn="work_buddy.health.checks.check_sidecar_service_embedding",
            on_fail="Embedding service is not running. Check sidecar logs.",
        ),
    ],
))

_register(ComponentDef(
    id="telegram",
    display_name="Telegram Bot",
    category="service",
    health_source="composite",
    sidecar_service="telegram",
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
    health_source="sidecar",
    sidecar_service="dashboard",
    check_sequence=[
        CheckStep(
            description="Dashboard service health endpoint (port 5127)",
            check_fn="work_buddy.health.checks.check_sidecar_service_dashboard",
            on_fail="Dashboard service is not running. Check sidecar logs.",
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
