"""Tests for the partition adapters: KnowledgePartition + IRSourcePartition + bootstrap."""

from __future__ import annotations

from work_buddy.index.model import ProjectionKind, PoolStrategy
from work_buddy.index.partitions.ir_source import IRSourcePartition
from work_buddy.knowledge.model import DirectionsUnit, SystemUnit, VaultUnit
from work_buddy.knowledge.partition import KnowledgePartition


def _synthetic_store():
    return {
        "consent/system": SystemUnit(
            path="consent/system", name="Consent System",
            description="Session-scoped approval grants",
            content={"summary": "SQLite consent with TTL.", "full": "The consent system uses SQLite."},
            tags=["consent", "permissions"], aliases=["approval", "grants"],
        ),
        "personal/pattern-a": VaultUnit(
            path="personal/pattern-a", name="Pattern A", description="a personal pattern",
            category="work_pattern", severity="HIGH",
            content={"summary": "fixture", "full": "personal fixture body"},
            tags=["metacognition"],
        ),
        "vault/writer": DirectionsUnit(
            path="vault/writer", name="Vault Writer", description="section-aware writes",
            trigger="agent writes to a vault note",
            content={"summary": "insert at a section", "full": "resolver logic"},
            tags=["vault"],
        ),
    }


class TestKnowledgePartition:
    def _part(self):
        store = _synthetic_store()
        return KnowledgePartition(store_loader=lambda: store), store

    def test_discover_yields_ref_per_unit_with_hash(self):
        part, store = self._part()
        refs = list(part.discover())
        assert {r.item_id for r in refs} == set(store)
        assert all(r.content_hash for r in refs)
        # stable across calls
        refs2 = list(part.discover())
        assert {r.item_id: r.content_hash for r in refs} == {r.item_id: r.content_hash for r in refs2}

    def test_parse_builds_document_with_projections_and_scope(self):
        part, _ = self._part()
        docs = part.parse("consent/system")
        assert len(docs) == 1
        d = docs[0]
        assert d.doc_id == "knowledge:consent/system"
        assert d.partition == "knowledge"
        assert d.fields["name"] == "Consent System"
        # content projection (passage) + aliases projection (label, list)
        assert "content" in d.projections
        assert "approval" in d.projections["aliases"].text
        assert d.metadata["scope"] == "system"
        assert d.metadata["kind"]  # SystemUnit kind populated

    def test_personal_unit_scope(self):
        part, _ = self._part()
        d = part.parse("personal/pattern-a")[0]
        assert d.metadata["scope"] == "personal"

    def test_no_aliases_no_alias_projection(self):
        part, _ = self._part()
        d = part.parse("vault/writer")[0]
        assert "aliases" not in d.projections  # DirectionsUnit fixture has none

    def test_projection_schema(self):
        part, _ = self._part()
        sch = part.projection_schema()
        assert sch["content"].kind == ProjectionKind.PASSAGE
        assert sch["aliases"].kind == ProjectionKind.LABEL
        assert sch["aliases"].pool == PoolStrategy.MAX

    def test_hydrate_returns_tiered_units(self):
        from work_buddy.index.model import Hit
        part, _ = self._part()
        hits = [Hit(doc_id="knowledge:consent/system", score=0.9)]
        out = part.hydrate(hits, depth="index")
        assert out and out[0]["path"] == "consent/system"
        assert out[0]["score"] == 0.9
        assert "name" in out[0]  # tiered fields present


class _FakeIRDoc:
    def __init__(self, doc_id, fields, dense_text="", display_text="", metadata=None, projections=None):
        self.doc_id = doc_id
        self.fields = fields
        self.dense_text = dense_text
        self.display_text = display_text
        self.metadata = metadata or {}
        self.projections = projections or {}


class _FakeIRSource:
    name = "conversation"

    def default_field_weights(self):
        return {"user_text": 1.5, "assistant_text": 1.0}

    def discover(self):
        return [("sess1", 100.0), ("sess2", 200.0)]

    def parse(self, item_id):
        return [_FakeIRDoc(
            doc_id=f"{item_id}:0", fields={"user_text": "hello"},
            dense_text="hello world span", display_text="a span",
            metadata={"start_time": "2026-01-01T00:00:00+00:00", "session_id": item_id},
        )]


class TestIRSourcePartition:
    def test_wraps_name_and_weights(self):
        p = IRSourcePartition(_FakeIRSource())
        assert p.name == "conversation"
        assert p.change_key == "mtime"
        assert p.field_weights()["user_text"] == 1.5

    def test_discover_normalizes_to_itemrefs(self):
        p = IRSourcePartition(_FakeIRSource())
        refs = list(p.discover())
        assert {r.item_id for r in refs} == {"sess1", "sess2"}
        assert {r.mtime for r in refs} == {100.0, 200.0}

    def test_parse_converts_ir_doc(self):
        p = IRSourcePartition(_FakeIRSource())
        docs = p.parse("sess1")
        d = docs[0]
        assert d.doc_id == "conversation:sess1:0"   # prefixed with partition
        assert d.partition == "conversation"
        assert d.fields["user_text"] == "hello"
        # bare dense_text → a content PASSAGE projection
        assert d.projections["content"].text == "hello world span"
        # ISO start_time → epoch timestamp (for recency)
        assert d.timestamp is not None and d.timestamp > 0

    def test_projection_schema_defaults_to_content_passage(self):
        p = IRSourcePartition(_FakeIRSource())
        sch = p.projection_schema()
        assert sch["content"].kind == ProjectionKind.PASSAGE


class _FakeDiscoveredFile:
    def __init__(self, item_id, abs_path, vault_id="v1", mtime=10.0):
        self.item_id = item_id
        self.source_path = item_id
        self.vault_id = vault_id
        self.mtime = mtime
        self.size = 0
        self.abs_path = str(abs_path)


class _FakeVaultSource:
    def __init__(self, files):
        self._files = files

    def discover(self):
        return (self._files, [])


class TestVaultChunkPartition:
    def test_discover_and_parse_chunks(self, tmp_path):
        from work_buddy.vault_index.partition import VaultChunkPartition

        md = tmp_path / "note.md"
        md.write_text(
            "# Title\n\nIntro text.\n\n## Section A\n\nAlpha body.\n\n## Section B\n\nBeta body.\n",
            encoding="utf-8",
        )
        src = _FakeVaultSource([_FakeDiscoveredFile("v1/note.md", md)])
        part = VaultChunkPartition(source=src)

        refs = list(part.discover())
        assert refs[0].item_id == "v1/note.md"
        assert part.change_key == "mtime"

        docs = part.parse("v1/note.md")
        assert len(docs) >= 2  # at least Section A + Section B chunks
        d = docs[0]
        assert d.partition == "vault"
        assert d.doc_id.startswith("vault:")
        assert "content" in d.projections
        assert d.metadata["vault_id"] == "v1"

    def test_unknown_item_returns_empty(self, tmp_path):
        from work_buddy.vault_index.partition import VaultChunkPartition
        part = VaultChunkPartition(source=_FakeVaultSource([]))
        list(part.discover())
        assert part.parse("nope") == []


class _FakeLifecycleSource:
    """A history-style IR source: discover() takes coverage, exposes lifecycle()."""

    name = "task_note"

    def __init__(self, states):
        # states: {item_id: state}; archived items only appear under coverage="all"
        self._states = dict(states)
        self.last_coverage = None

    def default_field_weights(self):
        return {"line": 2.0, "body": 1.0}

    def discover(self, days: int = 30, *, coverage: str = "active"):
        self.last_coverage = coverage
        items = []
        for iid, state in self._states.items():
            if coverage != "all" and state == "archived":
                continue
            items.append((iid, 100.0))
        return items

    def lifecycle(self, item_ids):
        return {iid: self._states.get(iid, "unknown") for iid in item_ids}

    def parse(self, item_id):
        return [_FakeIRDoc(
            doc_id=f"{item_id}", fields={"line": item_id},
            dense_text=f"note body for {item_id}", display_text=item_id,
            metadata={"note_uuid": item_id},
        )]


class TestIRSourcePartitionCoverage:
    """The generic coverage + lifecycle seam (extensible to any history source)."""

    def _states(self):
        return {"open1": "open", "done1": "done", "arch1": "archived"}

    def test_no_lifecycle_source_unchanged(self):
        # A plain source (no lifecycle, no coverage kwarg) behaves exactly as before.
        p = IRSourcePartition(_FakeIRSource())
        assert p.change_key == "mtime"
        refs = list(p.discover())
        assert all(r.content_hash is None for r in refs)  # mtime-only, no token
        d = p.parse("sess1")[0]
        assert "lifecycle_state" not in d.metadata

    def test_lifecycle_source_uses_hash_change_key(self):
        p = IRSourcePartition(_FakeLifecycleSource(self._states()))
        assert p.change_key == "hash"  # state can change without an mtime change

    def test_active_coverage_excludes_archived(self):
        src = _FakeLifecycleSource(self._states())
        p = IRSourcePartition(src, coverage="active")
        ids = {r.item_id for r in p.discover()}
        assert ids == {"open1", "done1"}  # archived withheld
        assert src.last_coverage == "active"

    def test_all_coverage_includes_archived(self):
        src = _FakeLifecycleSource(self._states())
        p = IRSourcePartition(src, coverage="all")
        ids = {r.item_id for r in p.discover()}
        assert ids == {"open1", "done1", "arch1"}  # archived now in the corpus
        assert src.last_coverage == "all"

    def test_change_token_folds_state_and_mtime(self):
        # A state transition (mtime unchanged) must change the item's change token.
        s = self._states()
        p_open = IRSourcePartition(_FakeLifecycleSource(s), coverage="all")
        tok_open = {r.item_id: r.content_hash for r in p_open.discover()}["open1"]

        s2 = dict(s, open1="done")  # same item, same mtime, new state
        p_done = IRSourcePartition(_FakeLifecycleSource(s2), coverage="all")
        tok_done = {r.item_id: r.content_hash for r in p_done.discover()}["open1"]

        assert tok_open and tok_done and tok_open != tok_done

    def test_parse_stamps_uniform_lifecycle_state(self):
        src = _FakeLifecycleSource(self._states())
        p = IRSourcePartition(src, coverage="all")
        p.discover()  # populates per-item states
        d = p.parse("arch1")[0]
        assert d.metadata["lifecycle_state"] == "archived"  # filterable by any query

    def test_configure_applies_coverage_from_config(self):
        from work_buddy.index.config import PartitionConfig
        src = _FakeLifecycleSource(self._states())
        p = IRSourcePartition(src)               # constructed with default "active"
        p.configure(PartitionConfig(name="task_note", coverage="all"))
        ids = {r.item_id for r in p.discover()}
        assert "arch1" in ids                    # config drove coverage → archived included


class TestPartitionConfigCoverage:
    def test_default_coverage_is_active(self):
        from work_buddy.index.config import PartitionConfig
        assert PartitionConfig(name="x").coverage == "active"

    def test_from_dict_reads_coverage(self):
        from work_buddy.index.config import PartitionConfig
        pc = PartitionConfig.from_dict("task_note", {"coverage": "all", "rrf_k": 30})
        assert pc.coverage == "all" and pc.rrf_k == 30

    def test_load_config_threads_coverage(self):
        from work_buddy.index.config import load_index_config
        cfg = load_index_config({"index": {"enabled": True, "partitions": {
            "task_note": {"coverage": "all"}}}})
        assert cfg.partition("task_note").coverage == "all"
        assert cfg.partition("unlisted").coverage == "active"  # safe default


class TestTaskNoteSourceCoverage:
    """Real-SQLite test of the task_note SOURCE change (the recall fix at the source)."""

    def _setup(self, tmp_path, monkeypatch):
        import sqlite3
        from work_buddy.obsidian.tasks.mutations import TASK_NOTES_DIR

        vault = tmp_path / "vault"
        notes_dir = vault / TASK_NOTES_DIR
        notes_dir.mkdir(parents=True)
        rows = [
            ("t-open", "uuid-open", "open", None),
            ("t-done", "uuid-done", "done", None),
            ("t-arch", "uuid-arch", "open", "2026-01-01T00:00:00+00:00"),  # archived
        ]
        for _, uuid, _, _ in rows:
            (notes_dir / f"{uuid}.md").write_text(f"# {uuid}\n\nbody\n", encoding="utf-8")

        db = tmp_path / "tasks.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE task_metadata "
                  "(task_id TEXT, note_uuid TEXT, state TEXT, archived_at TEXT)")
        c.executemany("INSERT INTO task_metadata VALUES (?,?,?,?)", rows)
        c.commit(); c.close()

        def _conn():
            cc = sqlite3.connect(db); cc.row_factory = sqlite3.Row; return cc

        monkeypatch.setattr("work_buddy.config.load_config",
                            lambda *a, **k: {"vault_root": str(vault), "ir": {}})
        monkeypatch.setattr("work_buddy.obsidian.tasks.store.get_connection", _conn)
        from work_buddy.ir.sources.task_notes import TaskNoteSource
        return TaskNoteSource()

    def test_default_coverage_excludes_archived(self, tmp_path, monkeypatch):
        src = self._setup(tmp_path, monkeypatch)
        stems = {p.split("\\")[-1].split("/")[-1] for p, _ in src.discover()}
        assert "uuid-open.md" in stems and "uuid-done.md" in stems
        assert "uuid-arch.md" not in stems  # live-IR behavior preserved

    def test_all_coverage_includes_archived(self, tmp_path, monkeypatch):
        src = self._setup(tmp_path, monkeypatch)
        stems = {p.split("\\")[-1].split("/")[-1] for p, _ in src.discover(coverage="all")}
        assert "uuid-arch.md" in stems  # archived note now discoverable

    def test_lifecycle_maps_state_with_archived_priority(self, tmp_path, monkeypatch):
        src = self._setup(tmp_path, monkeypatch)
        paths = [p for p, _ in src.discover(coverage="all")]
        life = src.lifecycle(paths)
        by_uuid = {p.replace("\\", "/").split("/")[-1]: s for p, s in life.items()}
        assert by_uuid["uuid-open.md"] == "open"
        assert by_uuid["uuid-done.md"] == "done"
        assert by_uuid["uuid-arch.md"] == "archived"  # archived_at wins over state


class TestBootstrap:
    def test_ensure_registers_core_partitions(self):
        from work_buddy.index.partition import get_partition_registry
        from work_buddy.index.partitions.bootstrap import ensure_partitions_registered
        ensure_partitions_registered()
        names = get_partition_registry().names()
        assert "knowledge" in names
        for ir_name in ("conversation", "projects", "chrome", "summary", "task_note"):
            assert ir_name in names
        # the redundant IR "docs" source is intentionally NOT a partition
        assert "docs" not in names
