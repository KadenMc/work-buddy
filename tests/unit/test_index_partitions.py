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
