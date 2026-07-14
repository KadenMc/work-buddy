"""K0 join tests for the three declarative truth workloads."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import work_buddy.truth.export as truth_export
import work_buddy.truth.migrations as truth_migrations
from work_buddy.storage.migrations import Migration
from work_buddy.truth.contracts import Actor
from work_buddy.truth.export import export_store
from work_buddy.truth.store import TruthStore

from .fixture_runner import WorkloadRunner, load_workload


FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "truth"
FIXTURE_PATHS = tuple(sorted(FIXTURE_DIR.glob("*.yaml")))
HUMAN = Actor("human", "fixture-human")


def _synthetic_v2(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE migration_v2_marker "
        "(id TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT 'synthetic')"
    )


def _v2_runner() -> truth_migrations._TruthMigrationRunner:
    return truth_migrations._TruthMigrationRunner(
        "truth",
        migrations=[
            Migration(
                1,
                "initial truth ledger schema",
                truth_migrations._m001_initial_schema,
            ),
            Migration(2, "synthetic fixture v2", _synthetic_v2),
        ],
    )


def _database_version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


@pytest.mark.parametrize(
    "fixture_path",
    FIXTURE_PATHS,
    ids=lambda path: path.stem,
)
def test_each_workload_runs_through_the_joined_k0_surface(
    tmp_path: Path,
    fixture_path: Path,
) -> None:
    fixture = load_workload(fixture_path)
    root = tmp_path / fixture_path.stem
    root.mkdir()
    store = TruthStore.create(root, fixture["profile"])

    result = WorkloadRunner(store, fixture).run()

    assert result.name == fixture["name"]
    assert result.restored_store.store_id == store.store_id
    assert result.export_result.record_count > 0


@pytest.mark.parametrize(
    "fixture_path",
    FIXTURE_PATHS,
    ids=lambda path: path.stem,
)
def test_each_workload_survives_frozen_v1_to_synthetic_v2_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_path: Path,
) -> None:
    fixture = load_workload(fixture_path)
    root = tmp_path / (fixture_path.stem + "-upgraded")
    root.mkdir()
    frozen_v1 = TruthStore.create(root, fixture["profile"])
    assert _database_version(frozen_v1.paths.db) == 1

    monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", _v2_runner())
    monkeypatch.setattr(truth_export, "SCHEMA_VERSION", 2)
    upgraded = TruthStore.open(root)

    snapshot = upgraded.paths.db.with_name("store.pre-v1.db")
    assert snapshot.is_file()
    assert _database_version(snapshot) == 1
    assert _database_version(upgraded.paths.db) == 2
    conn = upgraded.connect()
    try:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'migration_v2_marker'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()

    result = WorkloadRunner(upgraded, fixture).run()

    assert result.restored_store.store_id == upgraded.store_id
    assert _database_version(result.restored_store.paths.db) == 2


def test_real_export_hook_runs_once_after_commit_and_never_after_rollback(
    tmp_path: Path,
) -> None:
    fixture = load_workload(FIXTURE_DIR / "electricrag_supersession.yaml")
    root = tmp_path / "post-commit-export"
    root.mkdir()
    calls: list[str] = []

    def export_after_commit(store: TruthStore) -> None:
        calls.append(store.store_id)
        export_store(store)

    store = TruthStore.create(
        root,
        fixture["profile"],
        on_commit=export_after_commit,
    )
    assert calls == [store.store_id]
    assert store.paths.claims_export.is_file()
    initial = store.paths.claims_export.read_bytes()

    claim = store.propose_claim(
        proposition="The fixture export hook follows successful commits.",
        claim_kind="decision_outcome",
        structured={
            "decision": "export_hook",
            "outcome": "run_after_commit",
        },
        actor=HUMAN,
        record_id="d4000000000040008000000000000001",
        created_at="2026-07-14T13:00:00Z",
        status_at="2026-07-14T13:00:00Z",
    ).claim
    committed = store.paths.claims_export.read_bytes()
    assert calls == [store.store_id, store.store_id]
    assert committed != initial
    assert claim.id.encode("ascii") in committed

    with pytest.raises(RuntimeError, match="fixture rollback"):
        with store.write_transaction() as conn:
            store.propose_claim(
                proposition="This proposal must roll back.",
                claim_kind="decision_outcome",
                structured={
                    "decision": "rollback",
                    "outcome": "never_export",
                },
                actor=HUMAN,
                record_id="d4000000000040008000000000000002",
                created_at="2026-07-14T13:01:00Z",
                status_at="2026-07-14T13:01:00Z",
                conn=conn,
            )
            raise RuntimeError("fixture rollback")

    assert calls == [store.store_id, store.store_id]
    assert store.paths.claims_export.read_bytes() == committed
    assert store.get_claim("d4000000000040008000000000000002") is None
