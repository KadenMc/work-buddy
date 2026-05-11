"""Static topology for the control graph — domains, subsystems, and their edges.

This module is pure data with zero imports from the rest of work-buddy.
Adding a new domain or subsystem is a one-file change here; the
builder in :mod:`work_buddy.control.graph` overlays live state
(preferences, health, requirement results) onto these skeletons.

Components and requirements are NOT listed here — they are enumerated
from ``COMPONENT_CATALOG`` and ``REQUIREMENT_REGISTRY`` at build time.
This file only contains the user-facing grouping layer plus the
dependency edges between subsystems and the components they need.

Node id conventions:

    domain:*      user-facing top-level bucket
    subsystem:*   intermediate grouping (e.g. Daily Notes as a facet of Obsidian)

Each entry carries:

    id:                 node id
    label:              human-readable name
    description:        tooltip / explainer
    grouping_parents:   parents in the hierarchy (empty for domains)
    component_deps:     component IDs this node requires (dep edges)
    subsystem_deps:     other subsystem IDs this node requires (rare)
    requirement_ids:    requirement IDs that roll up to this subsystem
                        (intentional duplication — a req can live under
                         both its owning component and a subsystem)
    children_components:  which components roll up into this node directly
                          (vs being reached via dep edges). Used by domains
                          whose "natural children" are components rather
                          than subsystems (e.g. domain:browser-capture →
                          component:chrome_extension).
"""

from __future__ import annotations

from typing import TypedDict


class _DomainDef(TypedDict, total=False):
    id: str
    label: str
    description: str
    grouping_parents: list[str]
    component_deps: list[str]
    subsystem_deps: list[str]
    requirement_ids: list[str]
    children_components: list[str]


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

DOMAINS: list[_DomainDef] = [
    {
        "id": "domain:journal",
        "label": "Journal",
        "description": (
            "Daily journaling, running notes, and the task lifecycle built "
            "on top of Obsidian."
        ),
        "grouping_parents": [],
        "children_components": [],  # reached via subsystem:daily-notes, subsystem:task-lifecycle
    },
    {
        "id": "domain:notifications",
        "label": "Notifications",
        "description": (
            "Surfaces that push information to you: dashboard views, "
            "Obsidian modals, Telegram bot messages."
        ),
        "grouping_parents": [],
        "children_components": ["dashboard", "obsidian", "telegram"],
    },
    {
        "id": "domain:knowledge",
        "label": "Knowledge & Retrieval",
        "description": (
            "Persistent memory, semantic search, and vault-derived "
            "structured knowledge."
        ),
        "grouping_parents": [],
        "children_components": [],  # reached via subsystems
    },
    {
        "id": "domain:browser",
        "label": "Browser Integration",
        "description": (
            "The Chrome extension — snapshots open tabs, lets work-buddy "
            "triage and act on them (close, group, focus, open URLs)."
        ),
        "grouping_parents": [],
        "children_components": ["chrome_extension"],
    },
    {
        "id": "domain:calendar",
        "label": "Calendar",
        "description": "Calendar integration via the Obsidian Google Calendar plugin.",
        "grouping_parents": [],
        "children_components": ["google_calendar"],
    },
    {
        "id": "domain:email",
        "label": "Email",
        "description": (
            "Email triage via the thunderbird-work-buddy companion add-on. "
            "Read-only: collects recent unread messages, drops them into "
            "the triage Review pool, and lets work-buddy display individual "
            "messages in Thunderbird. No compose / move / delete."
        ),
        "grouping_parents": [],
        "children_components": ["thunderbird"],
    },
    {
        "id": "domain:runtime",
        "label": "Runtime",
        "description": (
            "work-buddy's own service processes plus the network plumbing "
            "that publishes them — the dashboard, the inter-agent "
            "messaging service, the embedding service, and the Tailscale "
            "tunnel that exposes the dashboard to remote devices."
        ),
        "grouping_parents": [],
        "children_components": ["dashboard", "messaging", "embedding", "tailscale"],
    },
    {
        "id": "domain:system",
        "label": "System Prerequisites",
        "description": (
            "The work-buddy sidecar daemon — the supervisor that keeps "
            "every other service alive. If it's down, nothing else "
            "observes or acts."
        ),
        "grouping_parents": [],
        # Only the sidecar daemon is a true system-wide prerequisite.
        # PostgreSQL is modeled as a dependency of component:hindsight only
        # (via ComponentDef.depends_on); it appears in the UI as a dep
        # chip under the Hindsight subsystem rather than a domain child.
        "children_components": ["sidecar"],
    },
    {
        "id": "domain:backups",
        "label": "Backups",
        "description": (
            "Off-machine snapshots of work-buddy's vital SQLite databases "
            "(task_metadata, projects, messages, threads), pushed to a "
            "user-owned private GitHub repo on a schedule. Local rolling "
            "snapshots run unconditionally; the GitHub push is the "
            "opt-in piece this component gates."
        ),
        "grouping_parents": [],
        "children_components": ["github_backups"],
    },
]


# ---------------------------------------------------------------------------
# Subsystems
# ---------------------------------------------------------------------------

SUBSYSTEMS: list[_DomainDef] = [
    # --- domain:journal ---
    {
        "id": "subsystem:daily-notes",
        "label": "Daily Notes",
        "description": (
            "Daily note file per day under the journal directory, with "
            "Log, Sign-In, and Running Notes sections."
        ),
        "grouping_parents": ["domain:journal"],
        "component_deps": ["obsidian"],
        "requirement_ids": [
            "obsidian/daily-note/plugin-enabled",
            "obsidian/daily-note/dir-exists",
            "obsidian/daily-note/log-section",
            "obsidian/daily-note/sign-in-section",
            "obsidian/daily-note/running-notes-section",
        ],
    },
    {
        "id": "subsystem:task-lifecycle",
        "label": "Task Lifecycle",
        "description": (
            "Master task list plus the Obsidian Tasks plugin, which "
            "work-buddy reads for task state and due dates."
        ),
        "grouping_parents": ["domain:journal"],
        "component_deps": ["obsidian"],
        "requirement_ids": [
            "obsidian/plugins/tasks-plugin",
            "obsidian/tasks/master-list-exists",
        ],
    },
    # --- domain:knowledge ---
    {
        "id": "subsystem:hindsight",
        "label": "Hindsight Memory",
        "description": (
            "Persistent personal memory via Hindsight. Requires a running "
            "PostgreSQL instance."
        ),
        "grouping_parents": ["domain:knowledge"],
        "component_deps": ["hindsight"],  # hindsight itself depends on postgresql (edge in COMPONENT_CATALOG)
    },
    {
        "id": "subsystem:embedding",
        "label": "Embedding Service",
        "description": (
            "Sentence-embedding service used by hybrid search across "
            "tasks, chats, commits, and the knowledge store."
        ),
        "grouping_parents": ["domain:knowledge"],
        "component_deps": ["embedding"],
    },
    {
        "id": "subsystem:obsidian-knowledge",
        "label": "Obsidian Knowledge Plugins",
        "description": (
            "Smart Connections and Datacore plugins, which expose the "
            "Obsidian vault as a queryable knowledge surface."
        ),
        "grouping_parents": ["domain:knowledge"],
        "component_deps": ["smart_connections", "datacore"],
    },
    # ------------------------------------------------------------------
    # Repository Setup — work-buddy's own config files and paths.
    #
    # These are checks for things you'd touch when first cloning the
    # repo or moving it: config files exist, repos_root points at the
    # right directory, timezone is configured, the data/ dir is
    # writable. Crucially does NOT include vault_root (that moved to
    # component:obsidian since it's the path to the vault, an Obsidian
    # concern) or API keys (those live under subsystem:credentials).
    # ------------------------------------------------------------------
    {
        "id": "subsystem:repository-setup",
        "label": "Repository Setup",
        "description": (
            "work-buddy's own configuration: config.yaml + "
            "config.local.yaml exist, repos_root points to a real "
            "directory, timezone is a valid IANA zone, the data/ "
            "directory is writable."
        ),
        "grouping_parents": ["domain:system"],
        "requirement_ids": [
            "core/config/config-yaml-exists",
            "core/config/config-local-exists",
            "core/config/repos-root",
            "core/config/timezone",
            "core/data/writable",
        ],
    },
    # ------------------------------------------------------------------
    # Credentials — API keys + secrets work-buddy needs to call out.
    # Currently just the Anthropic key. Future home for any other
    # service credentials we add (e.g. OpenAI, Telegram bot token if
    # it stays a credential rather than a Telegram-component req).
    # ------------------------------------------------------------------
    {
        "id": "subsystem:credentials",
        "label": "Credentials",
        "description": (
            "API keys and secrets work-buddy needs to call external "
            "services. Currently the Anthropic API key (read by "
            "work_buddy.llm.runner)."
        ),
        "grouping_parents": ["domain:system"],
        "requirement_ids": [
            "core/env/anthropic-api-key",
        ],
    },
]


def iter_static_nodes() -> list[_DomainDef]:
    """Return the concatenated domain + subsystem list."""
    return [*DOMAINS, *SUBSYSTEMS]
