"""Explicit provider seam between unified retrieval and personal authority."""

from __future__ import annotations

from typing import Protocol

from work_buddy.knowledge.model import VaultUnit
from work_buddy.knowledge.personal.legacy import configured_legacy_root
from work_buddy.knowledge.personal.store import PersonalKnowledgeStore


class PersonalKnowledgeProvider(Protocol):
    def load_units(self, *, force: bool = False) -> dict[str, VaultUnit]: ...

    def get_unit(self, path: str) -> VaultUnit | None: ...

    def invalidate(self) -> None: ...


class SQLitePersonalKnowledgeProvider:
    def __init__(self, store: PersonalKnowledgeStore | None = None) -> None:
        self.store = store or PersonalKnowledgeStore()
        self._cache: dict[str, VaultUnit] | None = None

    def load_units(self, *, force: bool = False) -> dict[str, VaultUnit]:
        if self._cache is not None and not force:
            return self._cache
        units: dict[str, VaultUnit] = {}
        for record in self.store.list_units(lifecycles=("active", "archived")):
            unit = self._to_unit(record)
            units[unit.path] = unit
        self._cache = units
        return units

    def get_unit(self, path: str) -> VaultUnit | None:
        record = self.store.get_unit(path)
        return self._to_unit(record) if record is not None else None

    def invalidate(self) -> None:
        self._cache = None

    @staticmethod
    def _to_unit(record: dict) -> VaultUnit:
        categories = list(record["categories"])
        return VaultUnit(
            path=record["current_path"],
            name=record["name"],
            description=record["description"],
            aliases=list(record["aliases"]),
            tags=list(record["tags"]),
            content={
                "summary": record["summary"],
                "full": record["body"] or record["summary"],
            },
            requires=list(record["requires"]),
            parents=list(record["parent_paths"]),
            children=list(record["child_paths"]),
            category=categories[0] if categories else "",
            severity=record["severity"],
            last_observed=record["last_observed"],
            observation_count=record["observation_count"],
            source_file=record["source_file"],
            unit_id=record["unit_id"],
            revision=record["current_revision"],
            path_aliases=list(record["path_aliases"]),
            categories=categories,
            references=list(record["reference_paths"]),
            lifecycle=record["lifecycle"],
            privacy_class=record["privacy_class"],
            disclosure_class=record["disclosure_class"],
            body_mode=record["body_mode"],
            document_binding_id=record["document_binding_id"] or "",
            document_store_id=record["document_store_id"] or "",
            document_id=record["document_id"] or "",
        )


class LegacyMarkdownPersonalKnowledgeProvider:
    """Read-only provider used until an import cohort atomically seals."""

    def __init__(self) -> None:
        self._cache: dict[str, VaultUnit] | None = None

    def load_units(self, *, force: bool = False) -> dict[str, VaultUnit]:
        if self._cache is not None and not force:
            return self._cache
        root = configured_legacy_root()
        if root is None:
            self._cache = {}
        else:
            from work_buddy.knowledge.vault_adapter import load_vault_units

            self._cache = load_vault_units(root)
        return self._cache

    def get_unit(self, path: str) -> VaultUnit | None:
        return self.load_units().get(path)

    def invalidate(self) -> None:
        self._cache = None


_PROVIDER: PersonalKnowledgeProvider | None = None
_PROVIDER_EXPLICIT = False


def get_personal_knowledge_provider() -> PersonalKnowledgeProvider:
    global _PROVIDER, _PROVIDER_EXPLICIT
    if (
        _PROVIDER_EXPLICIT
        and _PROVIDER is not None
        and not isinstance(_PROVIDER, LegacyMarkdownPersonalKnowledgeProvider)
    ):
        # Preserve explicit isolated-test/provider injection.  The built-in
        # legacy provider is deliberately excluded: installing it must never
        # override an externally sealed SQLite authority.
        return _PROVIDER
    if _PROVIDER_EXPLICIT:
        # An attempted built-in legacy override was ignored.  Any provider
        # selected below is automatic and must remain subject to rechecks.
        _PROVIDER_EXPLICIT = False
    # Re-evaluate the external installed seal on every adapter selection.  A
    # long-running process may have cached the legacy provider before another
    # process publishes cutover; that cache must never outlive authority.
    authority = PersonalKnowledgeStore.existing_authority()
    if authority == "sqlite":
        if not isinstance(_PROVIDER, SQLitePersonalKnowledgeProvider):
            _PROVIDER = SQLitePersonalKnowledgeProvider()
    elif not isinstance(_PROVIDER, LegacyMarkdownPersonalKnowledgeProvider):
        _PROVIDER = LegacyMarkdownPersonalKnowledgeProvider()
    return _PROVIDER


def set_personal_knowledge_provider(
    provider: PersonalKnowledgeProvider | None,
) -> None:
    """Install an authority provider (primarily for isolated tests)."""

    global _PROVIDER, _PROVIDER_EXPLICIT
    _PROVIDER = provider
    _PROVIDER_EXPLICIT = provider is not None


def invalidate_personal_knowledge_provider() -> None:
    if _PROVIDER is not None:
        _PROVIDER.invalidate()
