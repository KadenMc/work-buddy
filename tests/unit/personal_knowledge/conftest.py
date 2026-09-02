from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.knowledge.personal.provider import (
    SQLitePersonalKnowledgeProvider,
    set_personal_knowledge_provider,
)
from work_buddy.knowledge.personal.store import PersonalKnowledgeStore


@pytest.fixture
def personal_store(tmp_path: Path) -> PersonalKnowledgeStore:
    return PersonalKnowledgeStore(tmp_path / "personal_knowledge.db")


@pytest.fixture
def personal_provider(personal_store: PersonalKnowledgeStore):
    provider = SQLitePersonalKnowledgeProvider(personal_store)
    set_personal_knowledge_provider(provider)
    import work_buddy.knowledge.store as unified

    unified._VAULT_STORE = None
    yield provider
    unified._VAULT_STORE = None
    set_personal_knowledge_provider(None)
    from work_buddy.knowledge.index import invalidate_index

    invalidate_index()
