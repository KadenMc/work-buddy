"""Tests for index/store.py — IndexStore SQLite + FTS5 + blob vectors.

Uses a tmp_path DB; no embedding service. Vectors are synthetic numpy arrays.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from work_buddy.index.model import Document, Projection
from work_buddy.index.store import (
    IndexSchemaRepairRequired,
    IndexStore,
    UnsupportedIndexSchemaVersion,
)


@pytest.fixture
def store(tmp_path):
    value = IndexStore(tmp_path / "t-index.db")
    value.prepare_schema()
    return value


def _doc(doc_id, partition="knowledge", *, name="", body="", tags="", meta=None, ts=None):
    return Document(
        doc_id=doc_id, partition=partition,
        fields={"name": name, "body": body, "tags": tags},
        display_text=f"{name}: {body}",
        metadata=meta or {},
        timestamp=ts,
    )


class TestUpsertAndLexical:
    def test_upsert_and_fts_search(self, store):
        store.upsert_documents([
            _doc("knowledge:a", name="Consent System", body="approval grants ttl"),
            _doc("knowledge:b", name="Telegram Bot", body="mobile notifications inline keyboard"),
        ], item_id="seed")
        hits = store.search_lexical("approval grants")
        assert "knowledge:a" in hits
        assert hits["knowledge:a"] > 0

    def test_title_weighted_over_body(self, store):
        # 'alpha' in a's title, in b's body. Default weights title(3) > body(1).
        store.upsert_documents([
            _doc("knowledge:a", name="alpha", body="filler text"),
            _doc("knowledge:b", name="other", body="alpha appears in body only"),
        ], item_id="seed")
        hits = store.search_lexical("alpha")
        assert hits["knowledge:a"] >= hits["knowledge:b"]

    def test_fts_weights_override_reweights_body(self, store):
        # Same corpus as the default-weights test: 'alpha' in a's title, b's body.
        store.upsert_documents([
            _doc("knowledge:a", name="alpha", body="filler text"),
            _doc("knowledge:b", name="other", body="alpha appears in body only"),
        ], item_id="seed")
        # Body-favoring weights (title=1, body=3) invert the default title bias:
        # the body match (b) now ranks at least as high as the title match (a).
        hits = store.search_lexical("alpha", weights=(1.0, 3.0, 1.0))
        assert hits["knowledge:b"] >= hits["knowledge:a"]
        # weights=None falls back to the store default (title-leaning) — unchanged.
        default_hits = store.search_lexical("alpha", weights=None)
        assert default_hits["knowledge:a"] >= default_hits["knowledge:b"]

    def test_partition_scoping(self, store):
        store.upsert_documents([_doc("knowledge:a", name="shared term")], item_id="i1")
        store.upsert_documents(
            [_doc("vault:a", partition="vault", name="shared term")], item_id="i2"
        )
        hits = store.search_lexical("shared", partition="vault")
        assert set(hits) == {"vault:a"}

    def test_scope_prefix(self, store):
        store.upsert_documents([
            _doc("knowledge:tasks/x", name="triage flow"),
            _doc("knowledge:obsidian/y", name="triage flow"),
        ], item_id="i")
        hits = store.search_lexical("triage", scope="knowledge:tasks/")
        assert set(hits) == {"knowledge:tasks/x"}

    def test_empty_query_returns_empty(self, store):
        store.upsert_documents([_doc("knowledge:a", name="x")], item_id="i")
        assert store.search_lexical("") == {}
        assert store.search_lexical("  !! ") == {}

    def test_fts_refreshed_on_reupsert(self, store):
        store.upsert_documents([_doc("knowledge:a", name="original")], item_id="i")
        assert "knowledge:a" in store.search_lexical("original")
        store.upsert_documents([_doc("knowledge:a", name="replaced")], item_id="i")
        assert store.search_lexical("original") == {}
        assert "knowledge:a" in store.search_lexical("replaced")

    def test_reupsert_preserves_rowid_and_invalidates_stale_vectors(self, store):
        store.upsert_documents([_doc("knowledge:a", name="original")], item_id="i")
        store.upsert_vectors("content", [("knowledge:a", np.ones(4, dtype=np.float32))])
        conn = store._connect()
        try:
            before = conn.execute(
                "SELECT rowid FROM documents WHERE doc_id = 'knowledge:a'"
            ).fetchone()[0]
        finally:
            conn.close()

        store.upsert_documents([_doc("knowledge:a", name="replacement")], item_id="i")

        conn = store._connect()
        try:
            after = conn.execute(
                "SELECT rowid FROM documents WHERE doc_id = 'knowledge:a'"
            ).fetchone()[0]
            fts_rowid = conn.execute(
                "SELECT rowid FROM doc_fts WHERE doc_fts MATCH 'replacement'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert after == before == fts_rowid
        assert store.vector_count("knowledge", "content") == 0


class TestMetadataFilter:
    def test_equality_filter(self, store):
        store.upsert_documents([
            _doc("knowledge:a", name="alpha", meta={"kind": "system"}),
            _doc("knowledge:b", name="alpha", meta={"kind": "directions"}),
        ], item_id="i")
        hits = store.search_lexical("alpha", filters={"kind": "system"})
        assert set(hits) == {"knowledge:a"}

    def test_set_membership_filter(self, store):
        store.upsert_documents([
            _doc("knowledge:a", name="alpha", meta={"kind": "system"}),
            _doc("knowledge:b", name="alpha", meta={"kind": "directions"}),
            _doc("knowledge:c", name="alpha", meta={"kind": "capability"}),
        ], item_id="i")
        hits = store.search_lexical("alpha", filters={"kind": ["system", "capability"]})
        assert set(hits) == {"knowledge:a", "knowledge:c"}

    def test_load_documents_with_filter(self, store):
        store.upsert_documents([
            _doc("knowledge:a", name="alpha", meta={"scope": "system"}),
            _doc("knowledge:b", name="beta", meta={"scope": "personal"}),
        ], item_id="i")
        docs = store.load_documents(partition="knowledge", filters={"scope": "personal"})
        assert set(docs) == {"knowledge:b"}
        assert docs["knowledge:b"]["fields"]["name"] == "beta"


class TestVectors:
    def test_blob_roundtrip(self, store):
        store.upsert_documents([
            _doc("knowledge:a", name="a"), _doc("knowledge:b", name="b"),
        ], item_id="i")
        rng = np.random.default_rng(0)
        v_a = rng.normal(size=8).astype(np.float32)
        v_b = rng.normal(size=8).astype(np.float32)
        n = store.upsert_vectors("content", [("knowledge:a", v_a), ("knowledge:b", v_b)])
        assert n == 2
        loaded = store.load_all_vectors("knowledge", "content")
        assert loaded is not None
        mat, doc_ids = loaded
        assert mat.shape == (2, 8)
        assert doc_ids == ["knowledge:a", "knowledge:b"]
        # float16 round-trip tolerance
        idx = doc_ids.index("knowledge:a")
        assert np.allclose(mat[idx], v_a, atol=1e-2)

    def test_load_vectors_empty(self, store):
        assert store.load_all_vectors("knowledge", "content") is None

    def test_fk_cascade_deletes_vectors(self, store):
        store.upsert_documents([_doc("knowledge:a", name="a")], item_id="item1")
        store.upsert_vectors("content", [("knowledge:a", np.ones(4, dtype=np.float32))])
        assert store.vector_count("knowledge", "content") == 1
        store.delete_item_docs("item1", partition="knowledge")
        # vectors cascade-deleted with the document
        assert store.vector_count("knowledge", "content") == 0
        assert store.doc_count("knowledge") == 0
        # FTS row gone too
        assert store.search_lexical("a", partition="knowledge") == {}

    def test_pooled_projection_multiple_subvectors(self, store):
        # A label projection (e.g. aliases) stores MANY vectors per doc.
        store.upsert_documents([_doc("knowledge:a", name="a")], item_id="i")
        rng = np.random.default_rng(1)
        vecs = rng.normal(size=(3, 6)).astype(np.float32)  # 3 aliases
        store.upsert_vectors("aliases", [("knowledge:a", vecs)])
        loaded = store.load_all_vectors("knowledge", "aliases")
        assert loaded is not None
        mat, doc_ids = loaded
        assert mat.shape == (3, 6)
        assert doc_ids == ["knowledge:a", "knowledge:a", "knowledge:a"]
        # distinct-doc count is 1, not 3
        assert store.vector_count("knowledge", "aliases") == 1
        # re-upsert replaces (2 aliases now)
        store.upsert_vectors("aliases", [("knowledge:a", rng.normal(size=(2, 6)).astype(np.float32))])
        mat2, ids2 = store.load_all_vectors("knowledge", "aliases")
        assert mat2.shape == (2, 6)

    def test_docs_missing_vectors_worklist(self, store):
        store.upsert_documents([
            Document(doc_id="knowledge:a", partition="knowledge", fields={"name": "a"},
                     projections={"content": Projection(text="alpha body")}),
            Document(doc_id="knowledge:b", partition="knowledge", fields={"name": "b"},
                     projections={"content": Projection(text="beta body")}),
        ], item_id="i")
        work = store.docs_missing_vectors("knowledge", "content")
        assert {d for d, _ in work} == {"knowledge:a", "knowledge:b"}
        # encode one → it drops off the work-list
        store.upsert_vectors("content", [("knowledge:a", np.ones(4, dtype=np.float32))])
        work2 = store.docs_missing_vectors("knowledge", "content")
        assert {d for d, _ in work2} == {"knowledge:b"}

    def test_docs_missing_vectors_limit_and_database_side_count(self, store):
        store.upsert_documents([
            Document(
                doc_id=f"knowledge:{i}",
                partition="knowledge",
                fields={"name": str(i)},
                projections={"content": Projection(text=f"body {i}")},
            )
            for i in range(5)
        ] + [
            Document(
                doc_id="knowledge:empty",
                partition="knowledge",
                fields={"name": "empty"},
                projections={"content": Projection(text=[""])},
            )
        ], item_id="i")

        work = store.docs_missing_vectors("knowledge", "content", limit=2)
        assert len(work) == 2
        # Empty scalar/list projections are not actionable remaining work.
        assert store.missing_vector_count("knowledge", "content") == 5
        store.upsert_vectors(
            "content",
            [(doc_id, np.ones(4, dtype=np.float32)) for doc_id, _ in work],
        )
        assert store.missing_vector_count("knowledge", "content") == 3

    def test_docs_missing_vectors_rejects_non_positive_limit(self, store):
        with pytest.raises(ValueError, match="positive integer"):
            store.docs_missing_vectors("knowledge", "content", limit=0)


def _create_legacy_fts_database(path, *, malformed_fields: bool = False):
    """Create the pre-rowid FTS schema without importing private production SQL."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY,
            partition TEXT NOT NULL,
            item_id TEXT NOT NULL DEFAULT '',
            fields TEXT NOT NULL DEFAULT '{}',
            projections TEXT NOT NULL DEFAULT '{}',
            display_text TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL DEFAULT '',
            timestamp REAL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE doc_vectors (
            doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            projection TEXT NOT NULL,
            sub INTEGER NOT NULL DEFAULT 0,
            dim INTEGER NOT NULL,
            vector BLOB NOT NULL,
            PRIMARY KEY (doc_id, projection, sub)
        );
        CREATE TABLE indexed_items (
            item_id TEXT NOT NULL,
            partition TEXT NOT NULL DEFAULT '',
            mtime REAL NOT NULL DEFAULT 0,
            content_hash TEXT NOT NULL DEFAULT '',
            doc_count INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT NOT NULL,
            PRIMARY KEY (item_id, partition)
        );
        CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE VIRTUAL TABLE doc_fts USING fts5(
            doc_id UNINDEXED, title, body, tags
        );
        """
    )
    fields_a = "not-json" if malformed_fields else json.dumps(
        {"name": "canonical alpha", "body": "fresh searchable body", "tags": "one"}
    )
    conn.executemany(
        "INSERT INTO documents "
        "(doc_id, partition, item_id, fields, indexed_at) VALUES (?,?,?,?,?)",
        [
            ("knowledge:a", "knowledge", "item-a", fields_a, "then"),
            (
                "knowledge:b",
                "knowledge",
                "item-b",
                json.dumps({"name": "canonical beta", "body": "second body"}),
                "then",
            ),
        ],
    )
    # Deliberately use unrelated FTS rowids and stale text.  Migration must rebuild
    # from documents, not assume old FTS consistency or copy these rowids.
    conn.executemany(
        "INSERT INTO doc_fts(rowid, doc_id, title, body, tags) VALUES (?,?,?,?,?)",
        [
            (9001, "knowledge:a", "stale alpha", "obsolete", ""),
            (9002, "knowledge:b", "stale beta", "obsolete", ""),
        ],
    )
    conn.execute(
        "INSERT INTO doc_vectors(doc_id, projection, sub, dim, vector) VALUES (?,?,?,?,?)",
        ("knowledge:a", "content", 0, 2, np.ones(2, dtype=np.float16).tobytes()),
    )
    conn.execute(
        "INSERT INTO indexed_items VALUES (?,?,?,?,?,?)",
        ("item-a", "knowledge", 1.0, "hash", 1, "then"),
    )
    rowids = {
        row[0]: row[1]
        for row in conn.execute("SELECT doc_id, rowid FROM documents").fetchall()
    }
    conn.commit()
    conn.close()
    return rowids


class TestFtsSchemaMigration:
    def test_legacy_database_is_atomically_rebuilt_and_rowid_aligned(self, tmp_path):
        path = tmp_path / "legacy.db"
        original_rowids = _create_legacy_fts_database(path)

        migrated = IndexStore(path)
        migrated.prepare_schema()
        assert "knowledge:a" in migrated.search_lexical("fresh searchable")
        assert migrated.search_lexical("obsolete") == {}
        assert migrated.vector_count("knowledge", "content") == 1
        assert migrated.get_indexed_items("knowledge")["item-a"] == (1.0, "hash")

        conn = sqlite3.connect(str(path))
        try:
            fts_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'doc_fts'"
            ).fetchone()[0]
            aligned = conn.execute(
                "SELECT d.doc_id, d.rowid, f.rowid FROM doc_fts f "
                "JOIN documents d ON d.rowid = f.rowid ORDER BY d.doc_id"
            ).fetchall()
            version = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'fts_schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert "content='documents'" in fts_sql
        assert version == "2"
        assert {doc_id: doc_rowid for doc_id, doc_rowid, _ in aligned} == original_rowids
        assert all(doc_rowid == fts_rowid for _, doc_rowid, fts_rowid in aligned)

        # Reopening is idempotent, and deletion uses the synchronized trigger.
        reopened = IndexStore(path)
        assert reopened.delete_item_docs("item-a", partition="knowledge") == 1
        assert reopened.search_lexical("fresh searchable") == {}
        assert reopened.vector_count("knowledge", "content") == 0

    def test_same_named_malformed_trigger_is_repaired_before_write(self, tmp_path):
        path = tmp_path / "malformed-trigger.db"
        store = IndexStore(path)
        store.prepare_schema()
        store.upsert_documents(
            [_doc("knowledge:a", name="original")], item_id="item-a"
        )

        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(
                """
                DROP TRIGGER documents_fts_update;
                CREATE TRIGGER documents_fts_update
                AFTER UPDATE OF title, body, tags ON documents BEGIN
                    SELECT 1;
                END;
                """
            )
            conn.commit()
        finally:
            conn.close()

        # An ordinary write refuses the malformed layout. Only the explicit repair
        # may rebuild FTS from authoritative documents and install the canonical body.
        with pytest.raises(IndexSchemaRepairRequired):
            store.upsert_documents(
                [_doc("knowledge:a", name="replacement")], item_id="item-a"
            )
        store.prepare_schema()
        store.upsert_documents(
            [_doc("knowledge:a", name="replacement")], item_id="item-a"
        )

        assert store.search_lexical("original") == {}
        assert "knowledge:a" in store.search_lexical("replacement")

    def test_concurrent_explicit_prepare_waits_for_sqlite_writer(self, tmp_path):
        import threading
        import time

        path = tmp_path / "concurrent-legacy.db"
        _create_legacy_fts_database(path)
        locker = sqlite3.connect(str(path))
        # The production consolidated index already runs in WAL mode. Avoid
        # making this test about a concurrent DELETE->WAL journal conversion,
        # whose PRAGMA does not honor SQLite's ordinary busy wait.
        locker.execute("PRAGMA journal_mode=WAL")
        locker.execute("BEGIN IMMEDIATE")
        outcome: dict[str, object] = {}

        def open_store() -> None:
            try:
                # The ordinary timeout is intentionally much shorter than the
                # held SQLite lock. Explicit preparation has its own migration wait.
                candidate = IndexStore(path, busy_timeout_s=0.001)
                candidate.prepare_schema()
                outcome["count"] = candidate.doc_count()
            except Exception as exc:  # pragma: no cover - asserted below
                outcome["error"] = exc

        worker = threading.Thread(target=open_store)
        worker.start()
        time.sleep(0.1)
        locker.commit()
        locker.close()
        worker.join(timeout=10)

        assert not worker.is_alive()
        assert outcome == {"count": 2}

    def test_malformed_legacy_fields_do_not_block_the_migration(self, tmp_path):
        path = tmp_path / "malformed.db"
        _create_legacy_fts_database(path, malformed_fields=True)

        migrated = IndexStore(path)
        migrated.prepare_schema()
        # The malformed document remains in the authoritative table; only its
        # unparseable lexical projection is empty.  Healthy rows are still rebuilt.
        assert migrated.doc_count("knowledge") == 2
        assert "knowledge:b" in migrated.search_lexical("second body")

    def test_failed_migration_rolls_back_the_complete_legacy_layout(
        self, tmp_path, monkeypatch
    ):
        from work_buddy.index import store as store_mod

        path = tmp_path / "rollback.db"
        _create_legacy_fts_database(path)
        monkeypatch.setattr(store_mod, "_CREATE_FTS_SQL", "CREATE definitely invalid")

        with pytest.raises(sqlite3.OperationalError):
            IndexStore(path).prepare_schema()

        conn = sqlite3.connect(str(path))
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()
            }
            fts_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'doc_fts'"
            ).fetchone()[0]
            fts_count = conn.execute("SELECT count(*) FROM doc_fts").fetchone()[0]
            version = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'fts_schema_version'"
            ).fetchone()
        finally:
            conn.close()
        assert not {"title", "body", "tags"}.intersection(columns)
        assert "doc_id UNINDEXED" in fts_sql
        assert fts_count == 2
        assert version is None

    def test_status_on_missing_path_is_side_effect_free(self, tmp_path):
        path = tmp_path / "missing" / "index.db"
        snapshot = IndexStore(path).status_snapshot()

        assert snapshot["state"] == "absent"
        assert snapshot["partitions"] == []
        assert not path.parent.exists()

    def test_status_on_legacy_schema_does_not_repair_or_change_bytes(
        self, tmp_path, monkeypatch
    ):
        from work_buddy.index import store as store_mod

        path = tmp_path / "legacy-status.db"
        _create_legacy_fts_database(path)
        before = path.read_bytes()
        monkeypatch.setattr(
            store_mod,
            "_ensure_fts_schema",
            lambda _conn: pytest.fail("read-only status attempted schema repair"),
        )

        snapshot = IndexStore(path).status_snapshot()

        assert snapshot["state"] == "repair_required"
        assert snapshot["partitions"][0]["total_items"] == 2
        assert "did not run it" in snapshot["partitions"][0]["detail"]
        assert path.read_bytes() == before

    def test_ordinary_open_refuses_legacy_schema_until_explicit_repair(self, tmp_path):
        path = tmp_path / "legacy-refusal.db"
        _create_legacy_fts_database(path)

        with pytest.raises(IndexSchemaRepairRequired, match="never run it implicitly"):
            IndexStore(path).doc_count()

        IndexStore(path).prepare_schema()
        assert IndexStore(path).doc_count() == 2

    def test_scheduled_preparation_may_create_but_not_repair_existing_schema(self, tmp_path):
        fresh = IndexStore(tmp_path / "fresh.db")
        fresh.prepare_schema(repair_existing=False)
        assert fresh.status_snapshot()["state"] == "current"

        v1_path = tmp_path / "v1-scheduled.db"
        _create_legacy_fts_database(v1_path)
        before = v1_path.read_bytes()
        with pytest.raises(IndexSchemaRepairRequired, match="scheduled builds"):
            IndexStore(v1_path).prepare_schema(repair_existing=False)
        assert v1_path.read_bytes() == before

    def test_scheduled_preparation_recovers_crash_created_empty_file(self, tmp_path):
        path = tmp_path / "empty.db"
        path.write_bytes(b"")

        store = IndexStore(path)
        store.prepare_schema(repair_existing=False)

        assert store.status_snapshot()["state"] == "current"
        assert store.doc_count() == 0

    def test_scheduled_preparation_recovers_interrupted_fresh_bootstrap(
        self, tmp_path, monkeypatch
    ):
        from work_buddy.index import store as store_mod

        path = tmp_path / "partial-bootstrap.db"
        store = IndexStore(path)
        real_ensure = store_mod._ensure_fts_schema

        monkeypatch.setattr(
            store_mod,
            "_ensure_fts_schema",
            lambda _conn: (_ for _ in ()).throw(RuntimeError("synthetic interruption")),
        )
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            store.prepare_schema(repair_existing=False)
        assert store.status_snapshot()["state"] == "absent"

        monkeypatch.setattr(store_mod, "_ensure_fts_schema", real_ensure)
        store.prepare_schema(repair_existing=False)
        assert store.status_snapshot()["state"] == "current"

    def test_scheduled_preparation_rolls_back_mid_base_schema_failure(
        self, tmp_path, monkeypatch
    ):
        from work_buddy.index import store as store_mod

        path = tmp_path / "mid-schema-bootstrap.db"
        store = IndexStore(path)
        real_schema = store_mod._SCHEMA
        first_boundary = real_schema.index("CREATE INDEX")
        broken_schema = (
            real_schema[:first_boundary]
            + "SELECT definitely_missing_bootstrap_function();\n"
            + real_schema[first_boundary:]
        )

        monkeypatch.setattr(store_mod, "_SCHEMA", broken_schema)
        with pytest.raises(sqlite3.OperationalError, match="missing_bootstrap"):
            store.prepare_schema(repair_existing=False)
        assert store.status_snapshot()["state"] == "absent"

        monkeypatch.setattr(store_mod, "_SCHEMA", real_schema)
        store.prepare_schema(repair_existing=False)
        assert store.status_snapshot()["state"] == "current"

    def test_prepare_runs_repair_while_shared_writer_gate_is_held(
        self, tmp_path, monkeypatch
    ):
        from work_buddy.index import store as store_mod
        from work_buddy.utils.index_lock import is_locked

        path = tmp_path / "gated-repair.db"
        _create_legacy_fts_database(path)
        observed: list[bool] = []
        real_ensure = store_mod._ensure_fts_schema

        def observed_ensure(conn):
            observed.append(is_locked(path.parent / f"{path.name}.build"))
            return real_ensure(conn)

        monkeypatch.setattr(store_mod, "_ensure_fts_schema", observed_ensure)
        IndexStore(path).prepare_schema()

        assert observed == [True]

    def test_prepare_waits_for_existing_advisory_builder(self, tmp_path):
        import threading

        from work_buddy.index.locking import index_writer_gate

        path = tmp_path / "advisory-wait.db"
        _create_legacy_fts_database(path)
        entered = threading.Event()
        finished = threading.Event()

        def repair():
            entered.set()
            IndexStore(path).prepare_schema()
            finished.set()

        with index_writer_gate(path):
            worker = threading.Thread(target=repair)
            worker.start()
            assert entered.wait(timeout=5)
            assert not finished.wait(timeout=0.2)
        worker.join(timeout=10)

        assert not worker.is_alive()
        assert finished.is_set()
        assert IndexStore(path).status_snapshot()["state"] == "current"

    def test_concurrent_explicit_repairs_elect_one_migrator(self, tmp_path, monkeypatch):
        import threading
        import time

        from work_buddy.index import store as store_mod

        path = tmp_path / "repair-election.db"
        _create_legacy_fts_database(path)
        real_ensure = store_mod._ensure_fts_schema
        calls = {"n": 0}
        calls_lock = threading.Lock()
        start = threading.Barrier(3)

        def counted_ensure(conn):
            with calls_lock:
                calls["n"] += 1
            time.sleep(0.1)
            return real_ensure(conn)

        monkeypatch.setattr(store_mod, "_ensure_fts_schema", counted_ensure)
        errors: list[Exception] = []

        def repair():
            start.wait(timeout=5)
            try:
                IndexStore(path).prepare_schema()
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        workers = [threading.Thread(target=repair) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=10)

        assert not errors
        assert all(not worker.is_alive() for worker in workers)
        assert calls == {"n": 1}
        assert IndexStore(path).status_snapshot()["state"] == "current"

    def test_prepare_fails_closed_when_writer_gate_is_unavailable(
        self, tmp_path, monkeypatch
    ):
        import builtins

        from work_buddy.index import store as store_mod

        path = tmp_path / "ungated-repair.db"
        _create_legacy_fts_database(path)
        before = path.read_bytes()
        ensure_calls: list[bool] = []
        real_ensure = store_mod._ensure_fts_schema
        real_import = builtins.__import__

        def observed_ensure(conn):
            ensure_calls.append(True)
            return real_ensure(conn)

        def import_without_gate(name, *args, **kwargs):
            if name == "work_buddy.utils.index_lock":
                raise ImportError("synthetic missing writer gate")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(store_mod, "_ensure_fts_schema", observed_ensure)
        monkeypatch.setattr(builtins, "__import__", import_without_gate)
        with pytest.raises(RuntimeError, match="writer gate is required"):
            IndexStore(path).prepare_schema()

        assert ensure_calls == []
        assert path.read_bytes() == before

    def test_future_schema_is_preserved_and_fails_closed(self, tmp_path):
        path = tmp_path / "future.db"
        current = IndexStore(path)
        current.prepare_schema()
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "UPDATE index_meta SET value='3' WHERE key='fts_schema_version'"
            )
            conn.commit()
        finally:
            conn.close()
        before = path.read_bytes()

        with pytest.raises(UnsupportedIndexSchemaVersion, match="newer"):
            IndexStore(path).prepare_schema()

        assert path.read_bytes() == before
        assert IndexStore(path).status_snapshot()["state"] == "unsupported"

    def test_status_rejects_future_schema_before_assuming_v2_table_names(self, tmp_path):
        path = tmp_path / "future-renamed.db"
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO index_meta(key, value) VALUES "
                "('fts_schema_version', '3')"
            )
            conn.execute("CREATE TABLE future_documents (opaque BLOB)")
            conn.commit()
        finally:
            conn.close()
        before = path.read_bytes()

        snapshot = IndexStore(path).status_snapshot()

        assert snapshot["state"] == "unsupported"
        assert "newer than this binary" in snapshot["detail"]
        assert snapshot["partitions"] == []
        assert path.read_bytes() == before


class TestFtsDeletionPlans:
    def test_all_bulk_delete_paths_leave_fts_in_sync(self, store):
        now = 1000.0
        store.upsert_documents(
            [
                _doc("a:one", partition="a", name="itemtoken", ts=now - 10),
                _doc("a:two", partition="a", name="partitiontoken", ts=now - 20),
                _doc("b:old", partition="b", name="orphantoken", ts=now - 30),
                _doc("b:live", partition="b", name="livetoken", ts=now - 30),
            ],
            item_id="shared",
        )
        # Give the paths distinct item boundaries after one efficient batch insert.
        conn = store._connect()
        try:
            conn.execute("UPDATE documents SET item_id = 'partition' WHERE doc_id = 'a:two'")
            conn.execute("UPDATE documents SET item_id = 'old' WHERE doc_id = 'b:old'")
            conn.execute("UPDATE documents SET item_id = 'live' WHERE doc_id = 'b:live'")
            conn.commit()
        finally:
            conn.close()

        assert store.delete_item_docs("shared", partition="a") == 1
        assert store.search_lexical("itemtoken") == {}
        assert store.delete_partition("a") == 1
        assert store.search_lexical("partitiontoken") == {}
        store.mark_items_orphaned(["old"], "b")
        assert store.prune_orphans_older_than("b", now) == 1
        assert store.search_lexical("orphantoken") == {}
        assert "b:live" in store.search_lexical("livetoken")

    def test_item_delete_query_uses_the_documents_index(self, store):
        store.upsert_documents([_doc("knowledge:a", name="alpha")], item_id="item-a")
        conn = store._connect()
        try:
            plan = " ".join(
                row[3]
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN DELETE FROM documents "
                    "WHERE item_id = ? AND partition = ?",
                    ("item-a", "knowledge"),
                ).fetchall()
            )
            fts_rowid_plan = " ".join(
                row[3]
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT rowid FROM doc_fts WHERE rowid = ?",
                    (1,),
                ).fetchall()
            )
            fts_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'doc_fts'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert "idx_documents_item" in plan
        assert "item_id=? AND partition=?" in plan
        # FTS5 denotes an equality-constrained rowid lookup as virtual index ``:=``;
        # the legacy UNINDEXED-doc_id plan was bare ``INDEX 0:`` (a full scan).
        assert "INDEX 0:=" in fts_rowid_plan
        assert "UNINDEXED" not in fts_sql
        assert "content_rowid='rowid'" in fts_sql


class TestLedgerAndVersion:
    def test_indexed_items_ledger(self, store):
        store.mark_item_indexed("f.md", "knowledge", mtime=123.0, content_hash="abc", doc_count=2)
        items = store.get_indexed_items("knowledge")
        assert items["f.md"] == (123.0, "abc")

    def test_partition_item_ids_include_unmarked_documents(self, store):
        store.mark_item_indexed(
            "ledger.md", "knowledge", mtime=123.0, content_hash="abc", doc_count=0
        )
        store.upsert_documents(
            [_doc("knowledge:dangling", name="dangling")],
            item_id="dangling.md",
        )
        store.upsert_documents(
            [_doc("vault:other", partition="vault", name="other")],
            item_id="other.md",
        )

        assert store.partition_item_ids("knowledge") == ["dangling.md", "ledger.md"]

    def test_build_version_bump(self, store):
        assert store.build_version("knowledge") == 0
        assert store.bump_version("knowledge") == 1
        assert store.bump_version("knowledge") == 2
        assert store.build_version("knowledge") == 2
        # per-partition isolation
        assert store.build_version("vault") == 0

    def test_meta_roundtrip(self, store):
        assert store.get_meta("k") is None
        store.set_meta("k", "v")
        assert store.get_meta("k") == "v"

    def test_partitions_listing(self, store):
        store.upsert_documents([_doc("knowledge:a", name="a")], item_id="i")
        store.upsert_documents([_doc("vault:a", partition="vault", name="a")], item_id="j")
        assert store.partitions() == ["knowledge", "vault"]


class TestWriteContention:
    def test_write_rides_through_transient_lock(self, tmp_path, monkeypatch):
        # Another connection holds the write lock briefly; the store write must retry
        # through it and land — not die on the first ``database is locked``.
        # (sqlite3 connections are thread-bound, so the blocker lives entirely in
        # its own thread: connect → BEGIN IMMEDIATE → hold → commit.)
        import sqlite3
        import threading
        import time

        from work_buddy.index import store as store_mod

        st = IndexStore(tmp_path / "contend.db", busy_timeout_s=0.1)
        st.prepare_schema()
        st.set_meta("warm", "1")  # create the schema before contending
        monkeypatch.setattr(store_mod, "_WRITE_RETRY_DELAYS_S", (0.1, 0.2, 0.4, 0.8))

        acquired = threading.Event()

        def _hold_write_lock_briefly():
            conn = sqlite3.connect(str(st.db_path), timeout=5)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")  # take the DB write lock
                acquired.set()
                time.sleep(0.4)
                conn.commit()
            finally:
                conn.close()

        blocker = threading.Thread(target=_hold_write_lock_briefly)
        blocker.start()
        try:
            assert acquired.wait(5)
            st.set_meta("k", "v")  # would raise instantly without the retry
        finally:
            blocker.join()
        assert st.get_meta("k") == "v"

    def test_non_lock_errors_not_retried(self, tmp_path, monkeypatch):
        # Only contention retries; a real OperationalError surfaces immediately.
        import sqlite3
        from work_buddy.index import store as store_mod

        st = IndexStore(tmp_path / "fail.db")
        slept: list[float] = []
        monkeypatch.setattr(store_mod.time, "sleep", slept.append)

        def boom(self):
            raise sqlite3.OperationalError("no such table: nope")

        monkeypatch.setattr(IndexStore, "_connect", boom)
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            st.set_meta("k", "v")
        assert slept == []  # zero backoff sleeps → no retry happened
