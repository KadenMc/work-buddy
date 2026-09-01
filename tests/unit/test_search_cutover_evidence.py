"""Machine-derived search cutover and prospective-detachment receipts."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

import numpy as np
import pytest

from work_buddy.index.build import IndexBuilder
from work_buddy.index.config import IndexConfig
from work_buddy.index.cutover_checkpoint import checkpoint_search_cutover_databases
from work_buddy.index.cutover_evidence import SEARCH_DOMAINS, certify_search_cutover
from work_buddy.index.model import Document, ItemRef, Projection
from work_buddy.index.resident import ResidentCacheRegistry
from work_buddy.index.store import IndexStore
from work_buddy.vault_index.cutover import refresh_vault_for_prospective_seal
from work_buddy.vault_index.partition import VaultChunkPartition
from work_buddy.vault_index.source import FilesystemSource


class _Encoder:
    def __init__(self):
        self.document_batches = []

    def encode_documents(self, texts, *_args, **_kwargs):
        self.document_batches.append(list(texts))
        return np.ones((len(texts), 4), dtype=np.float32)


@dataclass
class _NativePartition:
    name: str
    pending: int = 0
    change_key: str = "hash"
    emit_documents: bool = True

    def field_weights(self):
        return {}

    def projection_schema(self):
        return {}

    def discover(self):
        return [ItemRef(item_id=f"{self.name}-1", content_hash=f"{self.name}-hash")]

    def parse(self, item_id):
        if not self.emit_documents:
            return []
        return [
            Document(
                doc_id=f"{self.name}:{item_id}",
                partition=self.name,
                fields={"title": f"{self.name} fixture", "body": "neutral record"},
            )
        ]

    def pending_search_event_count(self):
        return self.pending


class _Registry:
    def names(self):
        return list(SEARCH_DOMAINS) + ["vault"]


def _write(path, body="# Note\n\nneutral archive text\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _authority_db(path, table, column, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"CREATE TABLE {table} (singleton INTEGER PRIMARY KEY, {column} TEXT)"
        )
        connection.execute(
            f"INSERT INTO {table}(singleton,{column}) VALUES(1,?)", (value,)
        )


def _set_authority(path, table, column, value):
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE {table} SET {column}=? WHERE singleton=1", (value,))


def _fixture(tmp_path):
    vault = tmp_path / "vault"
    data = tmp_path / "data"
    database = data / "db"
    index_path = database / "index-consolidated.db"
    roots = {
        "journal": vault / "legacy" / "journal",
        "projects": vault / "legacy" / "projects",
        "contracts": vault / "legacy" / "contracts",
        "personal_knowledge": vault / "legacy" / "personal",
    }
    for domain, root in roots.items():
        _write(root / f"{domain}.md")
    _write(vault / "live.md", "# Live\n\ncurrent searchable text\n")
    cfg = {
        "vault_root": str(vault),
        "paths": {"data_root": str(data)},
        "obsidian": {
            "journal_dir": "legacy/journal",
            "exclude_folders": [],
        },
        "projects": {"markdown_dir": "legacy/projects"},
        "contracts": {"vault_path": "legacy/contracts"},
        "personal_knowledge": {"vault_path": "legacy/personal"},
        "vault_index": {"vaults": {"vault": {"path": str(vault)}}},
        "index": {"enabled": True, "db_path": str(index_path)},
    }
    authority = {
        "journal": (database / "journal_capture.db", "journal_authority_control", "mode"),
        "projects": (database / "projects.db", "project_authority_state", "authority"),
        "contracts": (database / "contracts.db", "contract_authority", "state"),
        "personal_knowledge": (
            database / "personal_knowledge.db",
            "personal_knowledge_authority",
            "authority",
        ),
    }
    legacy = {
        "journal": "legacy_compatibility",
        "projects": "legacy_markdown",
        "contracts": "legacy",
        "personal_knowledge": "legacy_markdown",
    }
    for name, declaration in authority.items():
        _authority_db(*declaration, legacy[name])
    return cfg, roots, authority, index_path


def test_prospective_purge_is_bounded_and_certified_before_seal(tmp_path):
    cfg, roots, _authority, index_path = _fixture(tmp_path)
    store = IndexStore(index_path)
    encoder = _Encoder()
    residents = ResidentCacheRegistry()
    before = VaultChunkPartition(
        source=FilesystemSource(cfg, authority_exclusions=())
    )
    IndexBuilder(
        store, encoder, before, residents=residents, use_lock=False
    ).build()
    assert len(store.get_indexed_items("vault")) == len(roots) + 1

    result = refresh_vault_for_prospective_seal(
        SEARCH_DOMAINS,
        cfg=cfg,
        index_config=IndexConfig(enabled=True, db_path=index_path),
        index_store=store,
        encoder=encoder,
        residents=residents,
    )
    assert result["deleted"] == len(roots)
    assert result["evidence"] == {
        "schema": "wb.legacy-root-detachment-evidence/v1",
        "detached_roots": len(roots),
        "detachment_active": True,
        "indexed_excluded_items": 0,
        "archive_discovery_fenced": True,
    }
    assert set(store.get_indexed_items("vault")) == {"vault/live.md"}

    partitions = {name: _NativePartition(name) for name in SEARCH_DOMAINS}
    for partition in partitions.values():
        IndexBuilder(
            store, encoder, partition, residents=residents, use_lock=False
        ).build()
    before_certification = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    receipt = certify_search_cutover(
        cfg=cfg,
        prospective_domains=SEARCH_DOMAINS,
        index_db_path=index_path,
        registry=_Registry(),
        partitions=partitions,
    )
    after_certification = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after_certification == before_certification
    assert receipt["detachment"]["schema"] == (
        "wb.legacy-root-detachment-evidence/v1"
    )
    assert receipt["detachment"]["mode"] == "prospective"
    assert receipt["detachment"]["ready"] is True
    assert receipt["search"]["schema"] == "wb.search-cutover-evidence/v1"
    assert receipt["search"]["ready"] is False
    assert str(tmp_path) not in json.dumps(receipt)

    with pytest.raises(ValueError, match="unsupported prospective legacy domains"):
        refresh_vault_for_prospective_seal(
            ["arbitrary_path"],
            cfg=cfg,
            index_config=IndexConfig(enabled=True, db_path=index_path),
            index_store=store,
            encoder=encoder,
            residents=residents,
        )


def test_targeted_prune_ignores_ordinary_churn_and_replay_invalidates(
    tmp_path, monkeypatch,
):
    from work_buddy.index.resident import ResidentCache
    from work_buddy.utils.index_lock import is_locked

    cfg, roots, authority, index_path = _fixture(tmp_path)
    store = IndexStore(index_path)
    encoder = _Encoder()
    residents = ResidentCacheRegistry()
    unexcluded = VaultChunkPartition(
        source=FilesystemSource(cfg, authority_exclusions=())
    )
    IndexBuilder(
        store, encoder, unexcluded, residents=residents, use_lock=False
    ).build()
    initial_version = store.build_version("vault")
    initial_last_build = store.get_meta("last_build:vault")
    live_ledger = store.get_indexed_items("vault")["vault/live.md"]

    # Represent an interrupted ordinary build: lexical documents landed under an
    # excluded item, but its change-ledger mark did not.
    dangling_item = "vault/legacy/journal/dangling.md"
    dangling_doc = Document(
        doc_id="vault:dangling",
        partition="vault",
        fields={"name": "dangling", "body": "dangling authority text"},
        projections={"content": Projection(text="dangling authority text")},
    )
    store.upsert_documents([dangling_doc], item_id=dangling_item)
    assert dangling_item not in store.get_indexed_items("vault")

    # Queue unrelated ordinary work after the last full build.  The cutover prune
    # must neither parse it nor mutate its existing ledger/document state.
    live_path = roots["journal"].parents[1] / "live.md"
    _write(live_path, "# Live\n\npending ordinary edit\n")
    os.utime(live_path, (live_ledger[0] + 10, live_ledger[0] + 10))
    new_path = live_path.parent / "new.md"
    _write(new_path, "# New\n\npending ordinary addition\n")

    # Projects is already durably sealed while Journal is prospective, proving
    # that the target set is the union of sustained and requested exclusions.
    _set_authority(*authority["projects"], "sqlite")

    cache = residents.register(
        "vault:content",
        ResidentCache(
            lambda: object(),
            lambda: str(store.build_version("vault")),
        ),
    )
    assert cache.get() is not None and cache.is_cached()
    encoder.document_batches.clear()

    gate = index_path.parent / f"{index_path.name}.build"
    identity = index_path.parent / f"{index_path.name}.vault"
    lock_observations = []
    original_bump = store.bump_version

    def observed_bump(partition):
        lock_observations.append((is_locked(gate), is_locked(identity)))
        return original_bump(partition)

    with monkeypatch.context() as patch:
        patch.setattr(store, "bump_version", observed_bump)
        patch.setattr(
            VaultChunkPartition,
            "discover",
            lambda _self: pytest.fail("targeted prune performed discovery"),
        )
        patch.setattr(
            VaultChunkPartition,
            "parse",
            lambda _self, _item_id: pytest.fail("targeted prune parsed a file"),
        )

        first = refresh_vault_for_prospective_seal(
            ["journal"],
            cfg=cfg,
            index_config=IndexConfig(enabled=True, db_path=index_path),
            index_store=store,
            encoder=encoder,
            residents=residents,
        )
        assert first["deleted"] == 3  # Journal + sustained Projects + dangling doc
        assert first["version"] == initial_version + 1
        assert first["operator"] == {
            "prospective_domains": ["journal"],
            "prospective_roots": 1,
            "sustained_roots": 1,
        }
        assert first["evidence"]["indexed_excluded_items"] == 0
        assert first["evidence"]["archive_discovery_fenced"] is True
        assert not cache.is_cached()

        # Replaying after all targets are already gone still publishes the version
        # and invalidation boundary needed after a crash between delete and bump.
        assert cache.get() is not None and cache.is_cached()
        replay = refresh_vault_for_prospective_seal(
            ["journal"],
            cfg=cfg,
            index_config=IndexConfig(enabled=True, db_path=index_path),
            index_store=store,
            encoder=encoder,
            residents=residents,
        )
        assert replay["deleted"] == 0
        assert replay["version"] == initial_version + 2
        assert replay["evidence"]["indexed_excluded_items"] == 0
        assert not cache.is_cached()

    assert lock_observations == [(True, True), (True, True)]
    assert encoder.document_batches == []
    assert store.get_meta("last_build:vault") == initial_last_build
    assert set(store.get_indexed_items("vault")) == {
        "vault/legacy/contracts/contracts.md",
        "vault/legacy/personal/personal_knowledge.md",
        "vault/live.md",
    }
    assert store.get_indexed_items("vault")["vault/live.md"] == live_ledger
    assert store.load_documents(
        partition="vault", doc_ids=[dangling_doc.doc_id]
    ) == {}
    assert store.search_lexical("current searchable", partition="vault")
    assert not store.search_lexical("pending ordinary", partition="vault")
    assert "vault/new.md" not in store.get_indexed_items("vault")

    # Once Journal is sealed, a later ordinary build sees the pending edit/addition
    # while the authority-aware source keeps both retired roots detached.
    _set_authority(*authority["journal"], "database_only")
    ordinary = IndexBuilder(
        store,
        encoder,
        VaultChunkPartition(source=FilesystemSource(cfg)),
        residents=residents,
        use_lock=False,
    ).build()
    assert ordinary["changed"] == 2
    assert set(store.get_indexed_items("vault")) == {
        "vault/legacy/contracts/contracts.md",
        "vault/legacy/personal/personal_knowledge.md",
        "vault/live.md",
        "vault/new.md",
    }
    assert store.search_lexical("pending ordinary edit", partition="vault")
    assert store.search_lexical("pending ordinary addition", partition="vault")
    assert store.get_meta("last_build:vault") != initial_last_build


def test_checkpoint_step_is_config_bounded_and_prepares_immutable_reads(tmp_path):
    cfg, _roots, authority, index_path = _fixture(tmp_path)
    # Initialize the consolidated schema, then keep one idle WAL connection open
    # per database so every sidecar remains observable until the bounded step.
    IndexStore(index_path).doc_count("vault")
    writers = []
    try:
        paths = [declaration[0] for declaration in authority.values()] + [index_path]
        for sequence, path in enumerate(paths):
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS checkpoint_fixture "
                "(sequence INTEGER PRIMARY KEY)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO checkpoint_fixture(sequence) VALUES(?)",
                (sequence,),
            )
            connection.commit()
            writers.append(connection)
        assert all(
            (path.parent / f"{path.name}-wal").stat().st_size > 0 for path in paths
        )

        receipt = checkpoint_search_cutover_databases(
            cfg=cfg,
            index_db_path=index_path,
        )
        assert receipt["schema"] == "wb.search-cutover-checkpoint-evidence/v1"
        assert receipt["requested_domains"] == list(SEARCH_DOMAINS)
        assert receipt["ready"] is True
        assert len(receipt["databases"]) == len(SEARCH_DOMAINS) + 1
        assert all(row["database_exists"] for row in receipt["databases"])
        assert all(row["wal_bytes_before"] > 0 for row in receipt["databases"])
        assert all(row["wal_bytes_after"] == 0 for row in receipt["databases"])
        assert all(row["ready"] for row in receipt["databases"])
        assert str(tmp_path) not in json.dumps(receipt)
    finally:
        for connection in writers:
            connection.close()

    with pytest.raises(ValueError, match="non-empty subset"):
        checkpoint_search_cutover_databases(
            cfg=cfg,
            domains=["arbitrary_path"],
            index_db_path=index_path,
        )


def test_postseal_receipt_requires_sustained_roots_parity_build_and_zero_lag(tmp_path):
    cfg, _roots, authority, index_path = _fixture(tmp_path)
    store = IndexStore(index_path)
    encoder = _Encoder()
    residents = ResidentCacheRegistry()
    partitions = {name: _NativePartition(name) for name in SEARCH_DOMAINS}

    sealed = {
        "journal": "database_only",
        "projects": "sqlite",
        "contracts": "native",
        "personal_knowledge": "sqlite",
    }
    for name, declaration in authority.items():
        _set_authority(*declaration, sealed[name])

    # The first authority-aware refresh removes all pre-existing archive rows.
    IndexBuilder(
        store,
        encoder,
        VaultChunkPartition(source=FilesystemSource(cfg, authority_exclusions=())),
        residents=residents,
        use_lock=False,
    ).build()
    sustained = FilesystemSource(cfg)  # derives the four roots from sealed DB rows
    IndexBuilder(
        store,
        encoder,
        VaultChunkPartition(source=sustained),
        residents=residents,
        use_lock=False,
    ).build()
    for partition in partitions.values():
        IndexBuilder(
            store, encoder, partition, residents=residents, use_lock=False
        ).build()

    receipt = certify_search_cutover(
        cfg=cfg,
        index_db_path=index_path,
        registry=_Registry(),
        partitions=partitions,
    )
    assert receipt["detachment"]["mode"] == "sustained"
    assert receipt["detachment"]["indexed_excluded_items"] == 0
    assert receipt["detachment"]["ready"] is True
    assert receipt["search"]["ready"] is True
    assert all(
        row["native_documents_visible"]
        for row in receipt["search"]["partitions"].values()
    )

    from work_buddy.utils import index_lock

    with index_lock.index_lock(index_path.parent / f"{index_path.name}.build"):
        in_flight = certify_search_cutover(
            cfg=cfg,
            index_db_path=index_path,
            registry=_Registry(),
            partitions=partitions,
        )
    assert in_flight["search"]["build_in_progress"] is True
    assert in_flight["search"]["ready"] is False
    assert in_flight["detachment"]["ready"] is False

    partitions["journal"].pending = 1
    lagged = certify_search_cutover(
        cfg=cfg,
        index_db_path=index_path,
        registry=_Registry(),
        partitions=partitions,
    )
    assert lagged["search"]["ready"] is False
    assert lagged["search"]["partitions"]["journal"]["pending_outbox"] == 1

    # Projects whose current source item resolves to no indexed native document
    # must not be certified merely because an external Co-work body is expected.
    partitions["journal"].pending = 0
    partitions["projects"].emit_documents = False
    IndexBuilder(
        store,
        encoder,
        partitions["projects"],
        residents=residents,
        use_lock=False,
    ).build(force=True)
    missing_project_body = certify_search_cutover(
        cfg=cfg,
        index_db_path=index_path,
        registry=_Registry(),
        partitions=partitions,
    )
    project_evidence = missing_project_body["search"]["partitions"]["projects"]
    assert missing_project_body["search"]["ready"] is False
    assert project_evidence["zero_document_items"] == 1
    assert project_evidence["delegated_or_empty_project_items"] == 1
    assert project_evidence["native_documents_visible"] is False
