"""Store loader for the unified knowledge system.

Loads system PromptUnit data from the file-per-unit markdown store at
``knowledge/store/`` (one ``.md`` file per unit), merges user patches from
``knowledge/store.local/``, and loads personal ``VaultUnit`` compatibility
objects from the personal-knowledge SQLite authority.

Three scopes:

* ``"system"`` — system documentation only (default, backward-compatible)
* ``"personal"`` — personal knowledge from the database provider only
* ``"all"`` — merged view of both stores
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from work_buddy.knowledge.model import (
    KnowledgeUnit,
    PromptUnit,
    VaultUnit,
    unit_from_dict,
    validate_dag,
)
from work_buddy.logging_config import get_logger
from work_buddy import paths

logger = get_logger(__name__)

_KNOWLEDGE_DIR = paths.asset_root() / "knowledge"
_STORE_DIR = _KNOWLEDGE_DIR / "store"
_LOCAL_DIR = _KNOWLEDGE_DIR / "store.local"

# Caches
_STORE: dict[str, PromptUnit] | None = None
_VAULT_STORE: dict[str, VaultUnit] | None = None
_VAULT_PROVIDER: object | None = None


def _load_json_dir(directory: Path) -> dict[str, dict[str, Any]]:
    """Load all .json files from a directory into a merged dict."""
    merged: dict[str, dict[str, Any]] = {}

    if not directory.is_dir():
        return merged

    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load %s: %s", path.name, e)
            continue

        if not isinstance(data, dict):
            logger.warning("Skipping %s: expected dict, got %s", path.name, type(data).__name__)
            continue

        for unit_path, unit_data in data.items():
            if unit_path in merged:
                logger.warning("Duplicate path %r: %s overrides previous", unit_path, path.name)
            merged[unit_path] = unit_data

    return merged


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge patch into base. Patch values win for scalars;
    dicts are merged recursively; lists are replaced (not appended)."""
    result = dict(base)
    for key, value in patch.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_system_raw() -> dict[str, dict[str, Any]]:
    """Load the system store's raw unit dicts from the file-per-unit store."""
    from work_buddy.knowledge.file_store import load_units_from_dir

    raw = load_units_from_dir(_STORE_DIR)
    logger.info("Loaded %d units from %s", len(raw), _STORE_DIR)
    return raw


def _derive_children(units: dict[str, PromptUnit]) -> None:
    """Populate each unit's ``children`` from other units' ``parents``.

    ``children`` is not stored — a unit's children are every unit that names
    it as a parent. Deriving it removes the parent/child symmetry-reconciliation
    class of bugs.
    """
    by_parent: dict[str, list[str]] = {}
    for path, unit in units.items():
        for parent in unit.parents:
            by_parent.setdefault(parent, []).append(path)
    for path, unit in units.items():
        unit.children = sorted(by_parent.get(path, []))


def load_vault(force: bool = False) -> dict[str, VaultUnit]:
    """Load personal units from the configured database authority provider.

    The historical function name is retained for callers.  It performs no
    filesystem discovery and has no vault fallback.
    """
    global _VAULT_STORE, _VAULT_PROVIDER  # noqa: PLW0603

    from work_buddy.knowledge.personal.provider import get_personal_knowledge_provider

    # Provider selection is authority-aware and must run even when content is
    # cached, otherwise a process that cached Markdown before cutover could
    # keep serving it after the installation latch is sealed.
    provider = get_personal_knowledge_provider()
    if _VAULT_STORE is not None and _VAULT_PROVIDER is provider and not force:
        return _VAULT_STORE

    _VAULT_STORE = provider.load_units(force=force)
    _VAULT_PROVIDER = provider
    if _VAULT_STORE:
        logger.info("Personal knowledge store ready: %d units", len(_VAULT_STORE))

    return _VAULT_STORE


def load_personal(force: bool = False) -> dict[str, VaultUnit]:
    """Preferred name for :func:`load_vault` after the SQLite cutover."""

    return load_vault(force)


def load_store(
    force: bool = False,
    scope: str = "system",
) -> dict[str, KnowledgeUnit]:
    """Load the knowledge store.

    1. Reads the file-per-unit markdown store from ``knowledge/store/``
    2. Applies user patches from ``knowledge/store.local/``
    3. Deserializes into typed PromptUnit objects
    4. Derives ``children`` from ``parents`` and validates DAG integrity
    5. Caches the result

    Args:
        force: Bypass cache and reload from disk.
        scope: Which store(s) to return:
               - ``"system"`` — system docs only (default, backward-compatible)
               - ``"personal"`` — personal database knowledge only
               - ``"all"`` — merged view of both

    Returns:
        Dict mapping unit path to KnowledgeUnit instance.
    """
    global _STORE  # noqa: PLW0603

    if scope == "personal":
        return load_vault(force)

    # Load system store if needed
    if _STORE is None or force:
        # Step 1: Load base store (file-per-unit markdown)
        raw = _load_system_raw()

        # Step 2: Apply local patches (always JSON; store.local/ is never
        # migrated to the file-per-unit substrate).
        local = _load_json_dir(_LOCAL_DIR)
        if local:
            for path, patch_data in local.items():
                if path in raw:
                    raw[path] = _deep_merge(raw[path], patch_data)
                    logger.debug("Applied local patch to %s", path)
                else:
                    raw[path] = patch_data
                    logger.debug("Added local-only unit %s", path)
            logger.info("Applied %d local patches", len(local))

        # Step 3: Deserialize
        units: dict[str, PromptUnit] = {}
        for path, data in raw.items():
            try:
                units[path] = unit_from_dict(path, data)  # type: ignore[assignment]
            except Exception as e:
                logger.warning("Failed to deserialize %s: %s", path, e)

        # Step 4: Derive ``children`` from ``parents`` — children is not
        # stored; a unit's children are every unit that names it as a parent.
        _derive_children(units)

        # Step 5: Validate DAG
        errors = validate_dag(units)  # type: ignore[arg-type]
        for err in errors:
            logger.warning("DAG validation: %s", err)

        _STORE = units
        logger.info(
            "Knowledge store ready: %d units (%d DAG warnings)",
            len(units),
            len(errors),
        )

    if scope == "all":
        vault = load_vault(force)
        if vault:
            merged: dict[str, KnowledgeUnit] = dict(_STORE)  # type: ignore[arg-type]
            merged.update(vault)
            return merged
        return _STORE  # type: ignore[return-value]

    return _STORE  # type: ignore[return-value]


def get_unit(path: str) -> KnowledgeUnit | None:
    """Look up a single unit by exact path.

    Checks system Markdown first, then the personal database provider.
    """
    unit = load_store().get(path)
    if unit is not None:
        return unit
    # Resolve current or historical logical aliases through the provider.
    if path.startswith("personal/"):
        from work_buddy.knowledge.personal.provider import get_personal_knowledge_provider
        return get_personal_knowledge_provider().get_unit(path)
    return None


def get_children(path: str) -> list[KnowledgeUnit]:
    """Get all direct children of a unit."""
    unit = get_unit(path)
    if unit is None:
        return []
    store = load_store(scope="all")
    return [store[c] for c in unit.children if c in store]


def get_subtree(prefix: str) -> dict[str, KnowledgeUnit]:
    """Get all units whose path starts with the given prefix."""
    store = load_store(scope="all")
    return {p: u for p, u in store.items() if p.startswith(prefix)}


def invalidate_vault() -> None:
    """Clear only the personal-provider cache so it reloads on next access."""
    global _VAULT_STORE, _VAULT_PROVIDER  # noqa: PLW0603
    _VAULT_STORE = None
    _VAULT_PROVIDER = None
    from work_buddy.knowledge.personal.provider import (
        invalidate_personal_knowledge_provider,
    )
    invalidate_personal_knowledge_provider()
    # Also invalidate the search index since vault content changed
    from work_buddy.knowledge.index import invalidate_index
    invalidate_index()


def invalidate_personal() -> None:
    """Preferred name for invalidating the personal authority projection."""

    invalidate_vault()


def invalidate_store() -> None:
    """Clear both caches so they reload on next access."""
    global _STORE, _VAULT_STORE, _VAULT_PROVIDER  # noqa: PLW0603
    _STORE = None
    _VAULT_STORE = None
    _VAULT_PROVIDER = None
    # Also invalidate the search index since store content changed
    from work_buddy.knowledge.index import invalidate_index
    invalidate_index()
