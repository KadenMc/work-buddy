from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.sources import ActorRef, SourceStore


@pytest.fixture
def source_store(tmp_path: Path) -> SourceStore:
    return SourceStore.create(tmp_path / "sources", inline_content_bytes=16)


@pytest.fixture
def tenant_id() -> str:
    return "tenant-00000001"


@pytest.fixture
def human(source_store: SourceStore, tenant_id: str) -> ActorRef:
    return ActorRef(source_store.authority_id, "profile-00000001", "human", tenant_id)


@pytest.fixture
def service(source_store: SourceStore, tenant_id: str) -> ActorRef:
    return ActorRef(source_store.authority_id, "journal-service", "service", tenant_id)


@pytest.fixture
def issuer(source_store: SourceStore, tenant_id: str) -> ActorRef:
    return ActorRef(source_store.authority_id, "dashboard-service", "service", tenant_id)


@pytest.fixture
def auth_sha() -> str:
    return "a" * 64
