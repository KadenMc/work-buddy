"""Bank bootstrap — create and configure the personal memory bank.

Call ``ensure_bank()`` on first use to create the bank with missions,
entity labels, directives, and mental models.  The bank configuration
is idempotent — safe to call repeatedly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.memory.client import get_bank_id, get_project_bank_id, get_client
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

ENTITY_LABELS = [
    "person", "project", "tool", "concept", "venue",
    "habit", "preference", "constraint", "blindspot",
    "relationship", "institution", "health_pattern",
]

DIRECTIVES = [
    ("evidence-over-opinion", "Distinguish observed facts from recommendations. Always show supporting evidence."),
    ("operational-language", "Use operational, non-moralizing language. Never label behaviour as lazy, bad, or wrong."),
    ("stable-patterns", "Prioritize stable recurring patterns over one-off moods or events."),
    ("exploration-vs-progress", "Do not mistake exploration for paper progress. Name the mode explicitly."),
    ("metacognition-patterns", "When evidence supports it, surface scope fusion, threshold inflation, infrastructure displacement, and insight hoarding."),
    ("gentle-reminders", "When repeated pattern evidence shows skipped meals, poor sleep, or burnout signals, offer gentle reminders — not lectures."),
]

MENTAL_MODELS = [
    ("self-profile", "Who is this user? Role, background, expertise, personal context."),
    ("work-patterns", "How does this user work? Recurring habits, productivity patterns, time management."),
    ("blindspots", "What recurring metacognitive patterns does this user exhibit?"),
    ("preferences", "How does this user prefer to interact with assistants? Communication style, output format, level of detail."),
    ("current-constraints", "What active limitations, deadlines, or commitments constrain this user right now?"),
]


def ensure_bank() -> None:
    """Create the personal bank if it doesn't exist, or update its config.

    Idempotent — safe to call on every session start.
    """
    client = get_client()
    bank_id = get_bank_id()
    cfg = load_config().get("hindsight", {})

    # Create or update the bank
    try:
        client.create_bank(
            bank_id=bank_id,
            name="Personal Digital Twin",
            mission=cfg.get("bank_mission", ""),
            retain_mission=cfg.get("retain_mission", ""),
            enable_observations=True,
            observations_mission=cfg.get("observations_mission", ""),
        )
        logger.info("Bank '%s' created/verified", bank_id)
    except Exception:
        # Bank may already exist — try updating config instead
        try:
            client.update_bank_config(
                bank_id=bank_id,
                mission=cfg.get("bank_mission", ""),
                retain_mission=cfg.get("retain_mission", ""),
                enable_observations=True,
                observations_mission=cfg.get("observations_mission", ""),
            )
            logger.info("Bank '%s' config updated", bank_id)
        except Exception:
            logger.exception("Failed to create or update bank '%s'", bank_id)
            raise

    # Ensure directives exist
    _ensure_directives_for(client, bank_id, DIRECTIVES)

    # Ensure mental models exist
    _ensure_mental_models_for(client, bank_id, MENTAL_MODELS)

    logger.info("Bank '%s' fully bootstrapped", bank_id)


def refresh_mental_models() -> list[str]:
    """Refresh all mental models from current observations.

    Returns list of model IDs that were refreshed.
    """
    client = get_client()
    bank_id = get_bank_id()
    refreshed = []

    try:
        models = client.list_mental_models(bank_id=bank_id)
        model_list = getattr(models, "items", None) or getattr(models, "models", []) or []
    except Exception:
        logger.exception("Failed to list mental models")
        return []

    for model in model_list:
        model_id = getattr(model, "id", None) or model.get("id") if isinstance(model, dict) else None
        if not model_id:
            continue
        try:
            client.refresh_mental_model(bank_id=bank_id, mental_model_id=model_id)
            refreshed.append(model_id)
            logger.info("Refreshed mental model '%s'", model_id)
        except Exception:
            logger.warning("Failed to refresh mental model '%s'", model_id, exc_info=True)

    return refreshed




# ═══════════════════════════════════════════════════════════════════
# Project memory bank
# ═══════════════════════════════════════════════════════════════════

PROJECT_DIRECTIVES = [
    ("project-scoped-facts", "Always tag extracted facts with the specific project they belong to. If a fact spans multiple projects, tag all relevant ones."),
    ("decisions-over-status", "Prioritize decisions, direction changes, and strategic context over routine status. 'We decided to use Qdrant instead of Pinecone' matters more than 'pushed 3 commits today'."),
    ("preserve-temporal-context", "Maintain temporal ordering of project events. When a decision reverses a prior one, note the reversal explicitly."),
    ("trajectory-signals", "Watch for trajectory-changing signals: supervisor feedback, scope changes, pivots, new constraints, abandoned approaches, and 'aha moments' that shift project direction."),
]

PROJECT_MENTAL_MODELS = [
    ("project-landscape", "What is the current state of all active projects? Which are progressing, stalled, or at risk?"),
    ("active-risks", "What risks, blockers, or concerns exist across active projects? What could derail progress?"),
    ("recent-decisions", "What significant decisions, pivots, or direction changes have been made recently across projects?"),
    ("inter-project-deps", "What dependencies, shared resources, or conflicts exist between projects?"),
]


def ensure_project_bank() -> None:
    """Create the project memory bank if it doesn't exist, or update its config.

    Idempotent — safe to call on every session start.
    """
    client = get_client()
    bank_id = get_project_bank_id()
    cfg = load_config().get("hindsight_projects", {})

    try:
        client.create_bank(
            bank_id=bank_id,
            name="Project Memory — Identity, State & Trajectory",
            mission=cfg.get("bank_mission", ""),
            retain_mission=cfg.get("retain_mission", ""),
            enable_observations=True,
            observations_mission=cfg.get("observations_mission", ""),
        )
        logger.info("Project bank '%s' created/verified", bank_id)
    except Exception:
        try:
            client.update_bank_config(
                bank_id=bank_id,
                mission=cfg.get("bank_mission", ""),
                retain_mission=cfg.get("retain_mission", ""),
                enable_observations=True,
                observations_mission=cfg.get("observations_mission", ""),
            )
            logger.info("Project bank '%s' config updated", bank_id)
        except Exception:
            logger.exception("Failed to create or update project bank '%s'", bank_id)
            raise

    _ensure_directives_for(client, bank_id, PROJECT_DIRECTIVES)
    _ensure_mental_models_for(client, bank_id, PROJECT_MENTAL_MODELS)

    logger.info("Project bank '%s' fully bootstrapped", bank_id)


def refresh_project_mental_models() -> list[str]:
    """Refresh all project mental models from current observations."""
    client = get_client()
    bank_id = get_project_bank_id()
    refreshed = []

    try:
        models = client.list_mental_models(bank_id=bank_id)
        model_list = getattr(models, "items", None) or getattr(models, "models", []) or []
    except Exception:
        logger.exception("Failed to list project mental models")
        return []

    for model in model_list:
        model_id = getattr(model, "id", None) or model.get("id") if isinstance(model, dict) else None
        if not model_id:
            continue
        try:
            client.refresh_mental_model(bank_id=bank_id, mental_model_id=model_id)
            refreshed.append(model_id)
            logger.info("Refreshed project mental model '%s'", model_id)
        except Exception:
            logger.warning("Failed to refresh project mental model '%s'", model_id, exc_info=True)

    return refreshed


# ---------------------------------------------------------------------------
# Generalized internal helpers
# ---------------------------------------------------------------------------

def _ensure_directives_for(client: Any, bank_id: str, directives: list[tuple[str, str]]) -> None:
    """Create directives if they don't already exist in the given bank."""
    try:
        existing = client.list_directives(bank_id=bank_id)
        existing_names = set()
        items = getattr(existing, "items", None) or getattr(existing, "directives", []) or []
        for d in items:
            name = getattr(d, "name", None) or (d.get("name") if isinstance(d, dict) else None)
            if name:
                existing_names.add(name)
    except Exception:
        existing_names = set()

    for name, content in directives:
        if name in existing_names:
            continue
        try:
            client.create_directive(bank_id=bank_id, name=name, content=content)
            logger.info("Created directive '%s' in bank '%s'", name, bank_id)
        except Exception:
            logger.warning("Failed to create directive '%s'", name, exc_info=True)


def _ensure_mental_models_for(client: Any, bank_id: str, models: list[tuple[str, str]]) -> None:
    """Create mental models if they don't already exist in the given bank."""
    try:
        existing = client.list_mental_models(bank_id=bank_id)
        existing_ids = set()
        items = getattr(existing, "items", None) or getattr(existing, "models", []) or []
        for m in items:
            mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
            if mid:
                existing_ids.add(mid)
    except Exception:
        existing_ids = set()

    for model_id, source_query in models:
        if model_id in existing_ids:
            continue
        try:
            client.create_mental_model(
                bank_id=bank_id,
                id=model_id,
                name=model_id.replace("-", " ").title(),
                source_query=source_query,
                trigger={"refresh_after_consolidation": True},
            )
            logger.info("Created mental model '%s' in bank '%s'", model_id, bank_id)
        except Exception:
            logger.warning("Failed to create mental model '%s'", model_id, exc_info=True)
