"""JSON + vault store loader for the unified knowledge system.

Loads system PromptUnit data from ``knowledge/store/*.json``, merges
user patches from ``knowledge/store.local/``, and optionally loads
personal VaultUnit data from the Obsidian vault.

Three scopes:

* ``"system"`` — system documentation only (default, backward-compatible)
* ``"personal"`` — personal knowledge from the vault only
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

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_STORE_DIR = _REPO_ROOT / "knowledge" / "store"
_LOCAL_DIR = _REPO_ROOT / "knowledge" / "store.local"

# Caches
_STORE: dict[str, PromptUnit] | None = None
_VAULT_STORE: dict[str, VaultUnit] | None = None


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


def _vault_dir() -> Path | None:
    """Resolve the configured vault directory for personal knowledge.

    Returns None if personal knowledge is disabled or the path doesn't exist.
    """
    from work_buddy.config import load_config

    cfg = load_config()
    pk = cfg.get("personal_knowledge", {})

    if not pk.get("enabled", True):
        return None

    vault_root = cfg.get("vault_root", "")
    if not vault_root:
        return None

    vault_subpath = pk.get("vault_path", "Meta/WorkBuddy")
    full = Path(vault_root) / vault_subpath

    return full if full.is_dir() else None


def load_vault(force: bool = False) -> dict[str, VaultUnit]:
    """Load personal knowledge units from the Obsidian vault.

    Lazy-loads and caches. Returns empty dict if vault is unavailable.
    """
    global _VAULT_STORE  # noqa: PLW0603

    if _VAULT_STORE is not None and not force:
        return _VAULT_STORE

    vdir = _vault_dir()
    if vdir is None:
        _VAULT_STORE = {}
        return _VAULT_STORE

    from work_buddy.knowledge.vault_adapter import load_vault_units

    _VAULT_STORE = load_vault_units(vdir)
    if _VAULT_STORE:
        logger.info("Vault store ready: %d personal units", len(_VAULT_STORE))

    return _VAULT_STORE


def load_store(
    force: bool = False,
    scope: str = "system",
) -> dict[str, KnowledgeUnit]:
    """Load the knowledge store.

    1. Reads all JSON files from ``knowledge/store/``
    2. Applies user patches from ``knowledge/store.local/``
    3. Deserializes into typed PromptUnit objects
    4. Validates DAG integrity
    5. Caches the result

    Args:
        force: Bypass cache and reload from disk.
        scope: Which store(s) to return:
               - ``"system"`` — system docs only (default, backward-compatible)
               - ``"personal"`` — personal vault knowledge only
               - ``"all"`` — merged view of both

    Returns:
        Dict mapping unit path to KnowledgeUnit instance.
    """
    global _STORE  # noqa: PLW0603

    if scope == "personal":
        return load_vault(force)

    # Load system store if needed
    if _STORE is None or force:
        # Step 1: Load base store
        raw = _load_json_dir(_STORE_DIR)
        logger.info("Loaded %d units from %s", len(raw), _STORE_DIR)

        # Step 2: Apply local patches
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

        # Step 4: Reconcile parent-child symmetry
        #
        # Auto-generated capabilities set parents but the parent's children
        # list may not include them (and vice versa). Fix both directions
        # so the DAG is always bidirectional regardless of authoring source.
        for path, unit in units.items():
            for parent_path in unit.parents:
                parent = units.get(parent_path)
                if parent is not None and path not in parent.children:
                    parent.children.append(path)
            for child_path in unit.children:
                child = units.get(child_path)
                if child is not None and child_path not in child.parents:
                    child.parents.append(path)

        # Sort children lists for deterministic ordering
        for unit in units.values():
            unit.children.sort()

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

    Checks system store first, then vault store for ``personal/`` paths.
    """
    unit = load_store().get(path)
    if unit is not None:
        return unit
    # Check vault for personal/ paths
    if path.startswith("personal/"):
        return load_vault().get(path)
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
    """Clear only the vault cache so it reloads on next access."""
    global _VAULT_STORE  # noqa: PLW0603
    _VAULT_STORE = None
    # Also invalidate the search index since vault content changed
    from work_buddy.knowledge.index import invalidate_index
    invalidate_index()


def invalidate_store() -> None:
    """Clear both caches so they reload on next access."""
    global _STORE, _VAULT_STORE  # noqa: PLW0603
    _STORE = None
    _VAULT_STORE = None
    # Also invalidate the search index since store content changed
    from work_buddy.knowledge.index import invalidate_index
    invalidate_index()
