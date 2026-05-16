"""Unit tests for the MarkdownDB abstraction (work_buddy.markdown_db).

Exercises the shape-agnostic orchestration — orphan handling, the
per-field drift loop, conflict resolution, dual-surface mutation,
materialization — against a toy subclass backed by an in-memory dict
store and an in-memory string "file". No real database, no real
filesystem layout beyond a tmp_path scratch file.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from work_buddy.markdown_db import (
    FieldSpec,
    InMemoryLwwLog,
    MarkdownDB,
    NullLwwLog,
    WriteProvenance,
    atomic_write_text,
    file_lock,
    mtime_utc,
)
from work_buddy.markdown_db.resolver import lww_markdown_wins
from work_buddy.markdown_db.types import Candidate, ParsedFileRow


# ════════════════════════════════════════════════════════════════════
# Toy store + toy MarkdownDB subclass.
# ════════════════════════════════════════════════════════════════════


class ToyStore:
    """In-memory CRUD store mimicking a work-buddy SQLite store module.

    Rows are dicts keyed by ``pk``. ``delete`` is soft (sets
    ``deleted_at``); ``query`` hides soft-deleted rows by default.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def query(self) -> list[dict[str, Any]]:
        return [
            dict(r) for r in self.rows.values() if r.get("deleted_at") is None
        ]

    def create(self, pk: str, **fields: Any) -> None:
        row = {"pk": pk, "deleted_at": None}
        row.update(fields)
        self.rows[pk] = row

    def update(self, pk: str, **fields: Any) -> None:
        if pk not in self.rows:
            raise KeyError(pk)
        self.rows[pk].update(fields)

    def delete(self, pk: str) -> None:
        if pk in self.rows:
            self.rows[pk]["deleted_at"] = "2026-01-01T00:00:00Z"

    def restore(self, pk: str) -> None:
        if pk in self.rows:
            self.rows[pk]["deleted_at"] = None


# A trivial line-oriented markdown layout:  ``pk | name | status | note``
_LINE_RE = re.compile(r"^(?P<pk>\S+)\s*\|\s*(?P<name>[^|]*)\|\s*"
                      r"(?P<status>[^|]*)\|\s*(?P<note>.*)$")


class ToyMarkdownDB(MarkdownDB):
    """Single-master-file MarkdownDB over a pipe-delimited line format."""

    table_name = "toy"
    pk_column = "pk"
    FIELDS = [
        FieldSpec("name", "name", "name"),
        FieldSpec("status", "status", "status", propagate_on_falsy=True),
        FieldSpec("note", "note", "note"),
    ]

    def __init__(self, master_file: Path, store: ToyStore, **kw: Any) -> None:
        super().__init__(store, **kw)
        self._master = Path(master_file)

    def markdown_path_for(self, pk: str) -> Path:
        return self._master

    def markdown_exists(self, pk: str) -> bool:
        # Single master file: existence is per-line.
        if not self._master.exists():
            return False
        return pk in self.parse_all_from_markdown()

    def parse_all_from_markdown(self) -> dict[str, ParsedFileRow]:
        out: dict[str, ParsedFileRow] = {}
        if not self._master.exists():
            return out
        for i, line in enumerate(
            self._master.read_text(encoding="utf-8").splitlines()
        ):
            line = line.strip()
            if not line:
                continue
            m = _LINE_RE.match(line)
            if not m:
                continue
            out[m["pk"]] = ParsedFileRow(
                pk=m["pk"],
                fields={
                    "name": m["name"].strip(),
                    "status": m["status"].strip(),
                    "note": m["note"].strip(),
                },
                line_number=i,
            )
        return out

    def write_entity_to_markdown(self, pk: str, fields: dict[str, Any]) -> None:
        parsed = self.parse_all_from_markdown()
        line = (
            f"{pk} | {fields.get('name') or ''} | "
            f"{fields.get('status') or ''} | {fields.get('note') or ''}"
        )
        lines = []
        replaced = False
        if self._master.exists():
            for raw in self._master.read_text(encoding="utf-8").splitlines():
                m = _LINE_RE.match(raw.strip())
                if m and m["pk"] == pk:
                    lines.append(line)
                    replaced = True
                elif raw.strip():
                    lines.append(raw)
        if not replaced:
            lines.append(line)
        atomic_write_text(self._master, "\n".join(lines) + "\n")


def _seed_master(path: Path, lines: list[str]) -> None:
    atomic_write_text(path, "\n".join(lines) + "\n")


# ════════════════════════════════════════════════════════════════════
# storage_helpers
# ════════════════════════════════════════════════════════════════════


def test_atomic_write_text_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "file.md"
    atomic_write_text(p, "hello\nworld\n")
    assert p.read_text(encoding="utf-8") == "hello\nworld\n"
    # No temp files left behind.
    assert list(p.parent.glob(".*tmp")) == []


def test_atomic_write_text_overwrites(tmp_path: Path) -> None:
    p = tmp_path / "f.md"
    atomic_write_text(p, "v1")
    atomic_write_text(p, "v2")
    assert p.read_text(encoding="utf-8") == "v2"


def test_file_lock_excludes_reentry(tmp_path: Path) -> None:
    target = tmp_path / "f.md"
    with file_lock(target):
        assert (target.with_name("f.md.lock")).exists()
        with pytest.raises(TimeoutError):
            with file_lock(target, timeout=0.2):
                pass
    # Released.
    assert not (target.with_name("f.md.lock")).exists()


def test_mtime_utc(tmp_path: Path) -> None:
    p = tmp_path / "f.md"
    assert mtime_utc(p) is None
    atomic_write_text(p, "x")
    mt = mtime_utc(p)
    assert mt is not None and mt.tzinfo is not None


# ════════════════════════════════════════════════════════════════════
# Resolver
# ════════════════════════════════════════════════════════════════════


def test_resolver_newer_ts_wins() -> None:
    spec = FieldSpec("name", "name", "name")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)
    md = Candidate("md-val", "markdown", ts=t0)
    store = Candidate("store-val", "store", ts=t1)
    assert lww_markdown_wins(spec, [md, store]).value == "store-val"


def test_resolver_markdown_wins_on_tie() -> None:
    spec = FieldSpec("name", "name", "name")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    md = Candidate("md-val", "markdown", ts=t0)
    store = Candidate("store-val", "store", ts=t0)
    assert lww_markdown_wins(spec, [md, store]).value == "md-val"


def test_resolver_markdown_wins_when_no_timestamps() -> None:
    spec = FieldSpec("name", "name", "name")
    md = Candidate("md-val", "markdown")
    store = Candidate("store-val", "store")
    assert lww_markdown_wins(spec, [md, store]).value == "md-val"


# ════════════════════════════════════════════════════════════════════
# apply_mutation
# ════════════════════════════════════════════════════════════════════


def test_apply_mutation_creates_both_surfaces(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, [])
    store = ToyStore()
    lww = InMemoryLwwLog()
    db = ToyMarkdownDB(master, store, lww=lww)

    db.apply_mutation(
        "p1",
        {"name": "Project One", "status": "active", "note": "hello"},
        provenance=WriteProvenance.mutation(frozenset({"user"}), "dashboard"),
    )

    # Store surface.
    rows = store.query()
    assert len(rows) == 1
    assert rows[0]["name"] == "Project One"
    assert rows[0]["status"] == "active"

    # Markdown surface.
    parsed = db.parse_all_from_markdown()
    assert parsed["p1"].fields["name"] == "Project One"
    assert parsed["p1"].fields["note"] == "hello"

    # LWW: 3 fields × 2 surfaces = 6 events.
    assert lww.event_count() == 6


def test_apply_mutation_updates_existing(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, ["p1 | Old Name | active | n"])
    store = ToyStore()
    store.create("p1", name="Old Name", status="active", note="n")
    db = ToyMarkdownDB(master, store)

    db.apply_mutation(
        "p1", {"name": "New Name"},
        provenance=WriteProvenance.mutation(frozenset({"agent"}), "agent"),
    )

    assert store.rows["p1"]["name"] == "New Name"
    # Untouched fields preserved on the markdown surface.
    parsed = db.parse_all_from_markdown()
    assert parsed["p1"].fields["name"] == "New Name"
    assert parsed["p1"].fields["status"] == "active"


def test_apply_mutation_rejects_unknown_field(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, [])
    db = ToyMarkdownDB(master, ToyStore())
    with pytest.raises(ValueError, match="unknown field"):
        db.apply_mutation(
            "p1", {"bogus": "x"},
            provenance=WriteProvenance.mutation(frozenset(), "dashboard"),
        )


# ════════════════════════════════════════════════════════════════════
# reconcile_drift — orphans
# ════════════════════════════════════════════════════════════════════


def test_reconcile_creates_orphan_in_markdown(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, ["p1 | Proj | active | note-text"])
    store = ToyStore()
    db = ToyMarkdownDB(master, store)

    report = db.reconcile_drift()

    assert report.created == ["p1"]
    assert store.rows["p1"]["name"] == "Proj"
    assert store.rows["p1"]["status"] == "active"


def test_reconcile_soft_deletes_orphan_in_store(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, [])
    store = ToyStore()
    store.create("ghost", name="Ghost", status="active", note="")
    db = ToyMarkdownDB(master, store)

    report = db.reconcile_drift()

    assert report.deleted == ["ghost"]
    assert store.rows["ghost"]["deleted_at"] is not None
    assert store.query() == []


# ════════════════════════════════════════════════════════════════════
# reconcile_drift — field drift
# ════════════════════════════════════════════════════════════════════


def test_reconcile_markdown_wins_pushes_to_store(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, ["p1 | Edited In Obsidian | active | n"])
    store = ToyStore()
    store.create("p1", name="Stale Name", status="active", note="n")
    db = ToyMarkdownDB(master, store)  # NullLwwLog → markdown wins

    report = db.reconcile_drift()

    assert store.rows["p1"]["name"] == "Edited In Obsidian"
    assert len(report.drift["name"]) == 1
    assert report.drift["name"][0]["winner"] == "markdown"


def test_reconcile_store_wins_writes_back_to_markdown(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, ["p1 | Old MD Name | active | n"])
    store = ToyStore()
    store.create("p1", name="Fresh Store Name", status="active", note="n")

    # InMemoryLwwLog with a store write NEWER than the markdown's mtime.
    lww = InMemoryLwwLog()
    future = datetime.now(timezone.utc) + timedelta(days=1)
    lww.record(
        table="toy", pk="p1", field="name", ts=future,
        provenance=WriteProvenance.mutation(frozenset({"user"}), "dashboard"),
        to_surface="store",
    )
    db = ToyMarkdownDB(master, store, lww=lww)

    report = db.reconcile_drift()

    # Store value won → written back into markdown.
    parsed = db.parse_all_from_markdown()
    assert parsed["p1"].fields["name"] == "Fresh Store Name"
    assert report.drift["name"][0]["winner"] == "store"


def test_reconcile_propagate_on_falsy_discipline(tmp_path: Path) -> None:
    """An empty markdown 'note' must NOT clear a populated store note,
    but an empty 'status' (propagate_on_falsy=True) propagates."""
    master = tmp_path / "master.md"
    _seed_master(master, ["p1 | Proj |  | "])  # empty status + empty note
    store = ToyStore()
    store.create("p1", name="Proj", status="active", note="keep-me")
    db = ToyMarkdownDB(master, store)

    db.reconcile_drift()

    # note: empty file value, propagate_on_falsy=False → store kept.
    assert store.rows["p1"]["note"] == "keep-me"
    # status: empty file value, propagate_on_falsy=True → propagated.
    assert store.rows["p1"]["status"] == ""


def test_reconcile_no_drift_when_in_sync(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, ["p1 | Proj | active | n"])
    store = ToyStore()
    store.create("p1", name="Proj", status="active", note="n")
    db = ToyMarkdownDB(master, store)

    report = db.reconcile_drift()

    assert not report.changed
    assert report.created == [] and report.deleted == []


def test_reconcile_none_vs_emptystring_not_drift(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, ["p1 | Proj | active | "])  # note empty
    store = ToyStore()
    store.create("p1", name="Proj", status="active", note=None)
    db = ToyMarkdownDB(master, store)

    report = db.reconcile_drift()
    assert not report.changed


# ════════════════════════════════════════════════════════════════════
# materialize_from_store
# ════════════════════════════════════════════════════════════════════


def test_materialize_dry_run_writes_nothing(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, [])
    store = ToyStore()
    store.create("p1", name="One", status="active", note="")
    store.create("p2", name="Two", status="past", note="")
    db = ToyMarkdownDB(master, store)

    result = db.materialize_from_store(dry_run=True)

    assert sorted(result["planned"]) == ["p1", "p2"]
    assert result["written"] == []
    assert db.parse_all_from_markdown() == {}


def test_materialize_apply_writes_missing_only(tmp_path: Path) -> None:
    master = tmp_path / "master.md"
    _seed_master(master, ["p1 | Existing | active | n"])
    store = ToyStore()
    store.create("p1", name="Existing", status="active", note="n")
    store.create("p2", name="New One", status="future", note="")
    db = ToyMarkdownDB(master, store)

    result = db.materialize_from_store(dry_run=False)

    assert result["planned"] == ["p2"]
    assert result["written"] == ["p2"]
    assert result["skipped"] == ["p1"]
    parsed = db.parse_all_from_markdown()
    assert parsed["p2"].fields["name"] == "New One"
    # p1 untouched.
    assert parsed["p1"].fields["name"] == "Existing"


# ════════════════════════════════════════════════════════════════════
# Subclass contract enforcement
# ════════════════════════════════════════════════════════════════════


def test_subclass_must_declare_fields(tmp_path: Path) -> None:
    class NoFields(MarkdownDB):
        table_name = "x"
        pk_column = "pk"

        def parse_all_from_markdown(self): return {}
        def write_entity_to_markdown(self, pk, fields): pass
        def markdown_path_for(self, pk): return tmp_path / "x"

    with pytest.raises(TypeError, match="FIELDS"):
        NoFields(ToyStore())


def test_subclass_must_set_table_identity(tmp_path: Path) -> None:
    class NoTable(MarkdownDB):
        FIELDS = [FieldSpec("a", "a", "a")]
        pk_column = ""

        def parse_all_from_markdown(self): return {}
        def write_entity_to_markdown(self, pk, fields): pass
        def markdown_path_for(self, pk): return tmp_path / "x"

    with pytest.raises(TypeError, match="table_name"):
        NoTable(ToyStore())


# ════════════════════════════════════════════════════════════════════
# WriteProvenance
# ════════════════════════════════════════════════════════════════════


def test_write_provenance_is_hashable_and_frozen() -> None:
    p = WriteProvenance.drift()
    assert p.actor == frozenset()
    assert p.process == "drift"
    # frozen + hashable
    {p}
    with pytest.raises(Exception):
        p.process = "x"  # type: ignore[misc]


def test_write_provenance_constructors() -> None:
    assert WriteProvenance.materialize().process == "materialize"
    assert WriteProvenance.migration().actor == frozenset({"system"})
    m = WriteProvenance.mutation(frozenset({"user"}), "dashboard")
    assert m.from_surface == "dashboard" and m.process == "mutation"
