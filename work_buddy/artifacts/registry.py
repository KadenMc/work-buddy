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

from typing import Any

from work_buddy.artifacts.protocol import Artifact, SweepResult

# Module-global registry keyed by artifact name.
_ARTIFACT_REGISTRY: dict[str, Artifact] = {}


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

    Args:
        dry_run: If True, prune surveys but doesn't mutate.
        name: If given, prune only the artifact with this registered
            name. If absent, prune all registered artifacts.

    Returns one :class:`SweepResult` per artifact pruned. Errors don't
    abort the sweep; failed artifacts come back with their ``error``
    field set.
    """
    if name is not None:
        artifact = _ARTIFACT_REGISTRY.get(name)
        if artifact is None:
            return [SweepResult(artifact_name=name, error=f"Unknown artifact {name!r}")]
        return [_safe_prune(artifact, dry_run)]
    return [_safe_prune(a, dry_run) for a in _ARTIFACT_REGISTRY.values()]


def artifact_registry_dump() -> dict[str, dict[str, Any]]:
    """Return the cross-backend introspection map.

    Used by ``artifact_registry()`` MCP capability so agents and
    operators can see at a glance what's registered, what each
    artifact's storage/lifecycle shape is, what capabilities each
    declares, and which operations are exposed via MCP.
    """
    return {name: a.describe() for name, a in sorted(_ARTIFACT_REGISTRY.items())}


def _reset_for_tests() -> None:
    """Clear the registry. Test-only — never call from production code."""
    _ARTIFACT_REGISTRY.clear()


def _safe_prune(artifact: Artifact, dry_run: bool) -> SweepResult:
    """Run ``.prune()`` and capture any escaping exception into the result."""
    try:
        return artifact.prune(dry_run=dry_run)
    except Exception as exc:  # pragma: no cover — defensive
        return SweepResult(artifact_name=artifact.name, error=str(exc))
