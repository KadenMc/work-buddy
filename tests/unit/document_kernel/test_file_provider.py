from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.cowork.source_observation import SourceObservationError
from work_buddy.document_kernel.file_provider import WorkBuddyFileImportProvider
from work_buddy.sources import (
    ActorRef,
    OriginRef,
    ProviderRegistry,
    SourceStore,
    source_capture_from_origin,
)


TENANT = "tenant-00000001"


def _provider(root: Path, *, maximum: int = 1024) -> WorkBuddyFileImportProvider:
    return WorkBuddyFileImportProvider(
        root,
        tenant_scope_id=TENANT,
        max_bytes=maximum,
    )


def test_registry_capture_retains_canonical_imported_file_and_unknown_author(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    exact = "A Unicode note: café 🧭\n".encode()
    (vault / "note.md").write_bytes(exact)
    store = SourceStore.create(tmp_path / "sources")
    principal = ActorRef(store.authority_id, "profile-00000001", "human", TENANT)
    provider = _provider(vault)
    registry = ProviderRegistry()
    registry.register(provider)

    ref = source_capture_from_origin(
        store,
        registry,
        provider_id=provider.provider_id,
        origin_ref=OriginRef(provider.provider_id, "note.md"),
        principal=principal,
        purpose="file_import",
        tenant_scope_id=TENANT,
        originating_surface="cowork",
    )

    item = store.get_item(ref)
    assert item is not None
    assert item.source_role == "imported_file"
    assert item.origin_ref == OriginRef(
        provider.provider_id,
        "note.md",
        container_id=provider.container_id,
    )
    representation = store.get_representation(item.primary_representation_id)
    assert representation is not None
    assert representation.media_type == "text/markdown"
    assert representation.encoding == "utf-8"
    conn = store.connect()
    try:
        assert store._read_representation_row(
            store._representation_row(conn, ref)
        ) == exact
        attributions = store.current_attributions(conn, ref)
    finally:
        conn.close()
    assert len(attributions) == 1
    assert attributions[0].role == "author"
    assert attributions[0].state == "unknown"


@pytest.mark.parametrize(
    "origin",
    [
        OriginRef("work-buddy-file-import", "note.md", part="paragraph-1"),
        OriginRef(
            "work-buddy-file-import",
            "note.md",
            coordinates={"line": "1"},
        ),
    ],
)
def test_whole_file_provider_rejects_invented_selectors(
    tmp_path: Path,
    origin: OriginRef,
) -> None:
    (tmp_path / "note.md").write_text("note", encoding="utf-8")
    with pytest.raises(SourceObservationError) as raised:
        _provider(tmp_path).canonicalize_origin(origin)
    assert raised.value.code == "source_selector_unsupported"


def test_markdown_capture_rejects_invalid_utf8(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_bytes(b"\xff\xfeinvalid")
    provider = _provider(tmp_path)
    with pytest.raises(SourceObservationError) as raised:
        provider.capture(
            OriginRef(provider.provider_id, "note.md"),
            "file_import",
        )
    assert raised.value.code == "source_encoding_invalid"
    assert raised.value.status == 415


def test_capture_is_bounded(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_bytes(b"too large")
    provider = _provider(tmp_path, maximum=3)
    with pytest.raises(SourceObservationError) as raised:
        provider.capture(OriginRef(provider.provider_id, "note.md"), "file_import")
    assert raised.value.code == "source_too_large"


def test_canonical_origin_cannot_escape_registered_root(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    provider = _provider(vault)
    with pytest.raises(SourceObservationError):
        provider.canonicalize_origin(OriginRef(provider.provider_id, "../outside.md"))
