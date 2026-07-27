"""Machine-level truth store registry tests."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from work_buddy.truth.export import (
    StoreIdentityCollision,
    export_store,
    import_store,
)
from work_buddy.truth.identity import new_id
from work_buddy.truth.registry import RegistryIdentityMismatch, TruthStoreRegistry
from work_buddy.truth.store import TruthStore


def _profile(store_id: str, *, title: str = "Registry test") -> dict[str, object]:
    return {
        "store_id": store_id,
        "profile": "test",
        "title": title,
        "allowed_claim_kinds": ["fact"],
        "required_fields": {},
        "gate": {
            "rejected_content": "retain",
            "confirmation_surfaces": ["cli"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
    }


def _store(root: Path, store_id: str | None = None) -> TruthStore:
    root.mkdir()
    identifier = store_id or new_id()
    return TruthStore.create(root, _profile(identifier))


def test_registry_schema_and_public_row_are_frozen(tmp_path: Path) -> None:
    registry = TruthStoreRegistry(tmp_path / "truth_registry.db")
    conn = sqlite3.connect(registry.db_path)
    try:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(truth_stores)")
        ]
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'uq_truth_stores_live_store_id'"
        ).fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert columns == [
        "path",
        "store_id",
        "profile",
        "title",
        "last_seen",
        "reachable",
        "layout",
        "document_surface_enabled",
        "allowed_document_classes_json",
        "feedback_capture",
        "document_count",
        "last_error",
        "path_key",
    ]
    assert "WHERE reachable = 1" in index_sql
    assert version == 4


@pytest.mark.skipif(os.name != "nt", reason="Windows path identity is case-insensitive")
def test_registry_preserves_filesystem_case_while_alias_lookup_is_case_insensitive(
    tmp_path: Path,
) -> None:
    registry = TruthStoreRegistry(tmp_path / "truth_registry.db")
    store = _store(tmp_path / "Reference Folder")

    registered = registry.register(Path(str(store.paths.sidecar).lower()))

    assert registered.path == store.paths.sidecar.resolve()
    assert registered.path.parent.parent.name == "Reference Folder"
    alias = registry.get_by_path(Path(str(store.paths.sidecar).upper()), refresh=False)
    assert alias is not None and alias.path == registered.path
    assert len(registry.list_stores(refresh=False)) == 1


def test_register_touch_and_access_validate_identity(tmp_path: Path) -> None:
    now = ["2026-07-16T20:00:00.000+00:00"]
    registry = TruthStoreRegistry(
        tmp_path / "truth_registry.db",
        clock=lambda: now[0],
    )
    store = _store(tmp_path / "scope")

    registered = registry.register(store.paths.root)
    assert registered.path == store.paths.sidecar.resolve()
    assert registered.store_id == store.store_id
    assert registered.profile == "test"
    assert registered.title == "Registry test"
    assert registered.last_seen == now[0]
    assert registered.reachable is True

    now[0] = "2026-07-16T20:01:00.000+00:00"
    touched = registry.touch(store)
    assert touched.last_seen == now[0]
    assert registry.paths_for_store_id(store.store_id) == (
        store.paths.sidecar.resolve(),
    )
    assert registry.open_store(store.store_id).store_id == store.store_id


def test_unreachable_history_does_not_advance_last_seen_and_can_revive(
    tmp_path: Path,
) -> None:
    now = ["2026-07-16T20:00:00.000+00:00"]
    registry = TruthStoreRegistry(
        tmp_path / "truth_registry.db",
        clock=lambda: now[0],
    )
    store = _store(tmp_path / "scope")
    store_id = store.store_id
    registered = registry.register(store)
    offline = tmp_path / "offline-sidecar"
    store.paths.sidecar.rename(offline)

    now[0] = "2026-07-16T20:05:00.000+00:00"
    unavailable = registry.get_by_path(store.paths.root)
    assert unavailable is not None
    assert unavailable.reachable is False
    assert unavailable.last_seen == registered.last_seen
    assert registry.paths_for_store_id(store_id) == ()

    offline.rename(store.paths.sidecar)
    revived = registry.get_by_path(store.paths.root)
    assert revived is not None
    assert revived.reachable is True
    assert revived.last_seen == now[0]


def test_duplicate_live_store_id_is_refused_but_offline_history_is_allowed(
    tmp_path: Path,
) -> None:
    store_id = new_id()
    registry = TruthStoreRegistry(tmp_path / "truth_registry.db")
    first = _store(tmp_path / "first", store_id)
    second = _store(tmp_path / "second", store_id)
    registry.register(first)

    with pytest.raises(StoreIdentityCollision, match="already reachable"):
        registry.register(second)

    offline = tmp_path / "first-offline"
    first.paths.sidecar.rename(offline)
    registry.list_stores(refresh=True)
    registered_second = registry.register(second)
    assert registered_second.reachable is True

    offline.rename(first.paths.sidecar)
    listed = registry.list_stores(refresh=True)
    assert not any(row.reachable for row in listed)
    with pytest.raises(StoreIdentityCollision, match="multiple paths"):
        registry.paths_for_store_id(store_id)
    assert not any(row.reachable for row in registry.list_stores(refresh=False))


def test_path_identity_change_fails_closed(tmp_path: Path) -> None:
    registry = TruthStoreRegistry(tmp_path / "truth_registry.db")
    root = tmp_path / "scope"
    original = _store(root)
    registry.register(original)

    moved = tmp_path / "old-sidecar"
    original.paths.sidecar.rename(moved)
    replacement = TruthStore.create(root, _profile(new_id()))

    with pytest.raises(RegistryIdentityMismatch, match="carries store_id"):
        registry.get_by_path(replacement.paths.root)
    row = registry.get_by_path(replacement.paths.root, refresh=False)
    assert row is not None
    assert row.reachable is False


def test_real_registry_unstubs_import_collision_preflight(tmp_path: Path) -> None:
    registry = TruthStoreRegistry(tmp_path / "truth_registry.db")
    source = _store(tmp_path / "source")
    registry.register(source)
    exported = export_store(source)
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(StoreIdentityCollision, match="already registered"):
        import_store(exported.path, target, registry=registry)
    assert not (target / ".wbuddy" / "cowork").exists()


def test_canonical_sidecar_projection_and_atomic_relocation(tmp_path: Path) -> None:
    registry = TruthStoreRegistry(tmp_path / "truth_registry.db")
    source_root = tmp_path / "source"
    source = _store(source_root)
    registered = registry.register(source)
    store_id = source.store_id
    assert registered.layout == "wbuddy_cowork_v1"
    assert registered.document_surface_enabled is False

    target_root = tmp_path / "target"
    target_root.mkdir()
    canonical = target_root / ".wbuddy" / "cowork"
    canonical.parent.mkdir()
    source.paths.sidecar.rename(canonical)
    relocated = registry.relocate(
        source.paths.sidecar,
        canonical,
        store_id=store_id,
    )

    assert relocated.path == canonical.resolve()
    assert relocated.layout == "wbuddy_cowork_v1"
    assert registry.get_by_path(source.paths.sidecar, refresh=False) is None
    assert registry.paths_for_store_id(store_id) == (canonical.resolve(),)
