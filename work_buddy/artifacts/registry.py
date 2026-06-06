"""Global registry of all configured Artifacts.

Each consumer module registers exactly one ``Artifact`` describing its
storage, lifecycle, and provenance. The registry is the single source
of truth for cross-backend operations:

* ``sweep_all(dry_run)`` is what ``artifact_cleanup`` MCP capability
  drives off of — iterates every registered artifact and calls
  ``.prune()``.
* ``artifact_registry_dump()`` powers the ``artifact_registry`` MCP
  capability — returns each artifact's introspection record.

Registration is idempotent by name: re-registering the same name
overwrites the previous entry. (This is useful for tests and for
modules that get reloaded.)
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from work_buddy.artifacts.protocol import Artifact, SweepResult

logger = logging.getLogger(__name__)

# Module-global registry keyed by artifact name.
_ARTIFACT_REGISTRY: dict[str, Artifact] = {}

# Modules whose import triggers an Artifact registration. Imported lazily
# the first time sweep_all is called (or via ensure_consumers_loaded())
# so that nobody pays the cost unless cleanup actually runs. Each entry
# is a fully-qualified module path.
_CONSUMER_MODULES: tuple[str, ...] = (
    "work_buddy.collectors.chrome_ledger",
    "work_buddy.llm.cache",
    "work_buddy.journal_backlog.segmentation_cache",
    "work_buddy.llm.escalation_log",
    "work_buddy.messaging.models",
    "work_buddy.agent_session",
    "work_buddy.llm.claude_code_usage.rollup",
    "work_buddy.notifications.store",
    "work_buddy.llm.queue",
    "work_buddy.conversation_observability",
    "work_buddy.summarization",
    "work_buddy.inference.metrics_store",
)

_consumers_loaded = False


def ensure_consumers_loaded() -> None:
    """Import every consumer module to trigger its Artifact registration.

    Idempotent — only imports modules once. Errors are logged as
    warnings but don't abort: a single broken consumer shouldn't take
    down the cleanup tick.
    """
    global _consumers_loaded
    if _consumers_loaded:
        return
    for mod_path in _CONSUMER_MODULES:
        try:
            importlib.import_module(mod_path)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "Failed to import consumer module %s during artifact "
                "registry load: %s",
                mod_path,
                exc,
            )
    _consumers_loaded = True


def register_artifact(artifact: Artifact) -> None:
    """Register (or replace) an artifact by name.

    Consumers should call this at module-import time so the artifact
    is available the first time the cleanup tick runs.
    """
    _ARTIFACT_REGISTRY[artifact.name] = artifact


def get_artifact(name: str) -> Artifact | None:
    """Return a registered artifact, or None if not registered."""
    return _ARTIFACT_REGISTRY.get(name)


def list_artifact_names() -> list[str]:
    """Return all registered artifact names, sorted."""
    return sorted(_ARTIFACT_REGISTRY)


def sweep_all(
    dry_run: bool = True, *, name: str | None = None
) -> list[SweepResult]:
    """Run ``.prune()`` against every registered artifact (or one).

    Triggers consumer-module imports first so all 11 artifacts are
    registered before the sweep runs.

    Args:
        dry_run: If True, prune surveys but doesn't mutate.
        name: If given, prune only the artifact with this registered
            name. If absent, prune all registered artifacts.

    Returns one :class:`SweepResult` per artifact pruned. Errors don't
    abort the sweep; failed artifacts come back with their ``error``
    field set.
    """
    ensure_consumers_loaded()
    if name is not None:
        artifact = _ARTIFACT_REGISTRY.get(name)
        if artifact is None:
            return [SweepResult(artifact_name=name, error=f"Unknown artifact {name!r}")]
        return [_safe_prune(artifact, dry_run)]
    return [_safe_prune(a, dry_run) for a in _ARTIFACT_REGISTRY.values()]


def artifact_registry_dump() -> dict[str, dict[str, Any]]:
    """Return the cross-backend introspection map.

    Triggers consumer-module imports first so all 11 artifacts appear.

    Used by ``artifact_registry()`` MCP capability so agents and
    operators can see at a glance what's registered, what each
    artifact's storage/lifecycle shape is, what capabilities each
    declares, and which operations are exposed via MCP.
    """
    ensure_consumers_loaded()
    return {name: a.describe() for name, a in sorted(_ARTIFACT_REGISTRY.items())}


def _reset_for_tests() -> None:
    """Clear the registry. Test-only — never call from production code."""
    global _consumers_loaded
    _ARTIFACT_REGISTRY.clear()
    _consumers_loaded = False


def _safe_prune(artifact: Artifact, dry_run: bool) -> SweepResult:
    """Run ``.prune()`` and capture any escaping exception into the result."""
    try:
        return artifact.prune(dry_run=dry_run)
    except Exception as exc:  # pragma: no cover — defensive
        return SweepResult(artifact_name=artifact.name, error=str(exc))
