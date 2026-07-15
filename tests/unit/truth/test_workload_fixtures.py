"""Truth-engine join tests for the three declarative workloads."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import work_buddy.truth.export as truth_export
import work_buddy.truth.migrations as truth_migrations
from work_buddy.storage.migrations import Migration
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import truth_uri
from work_buddy.truth.profiles import dump_profile
from work_buddy.truth.store import TruthStore

from .fixture_runner import WorkloadRunner, load_workload


FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "truth"
FIXTURE_PATHS = tuple(sorted(FIXTURE_DIR.glob("*.yaml")))
FROZEN_V1_DIR = FIXTURE_DIR / "frozen_v1"
FROZEN_STORE_ID = "f1000000000040008000000000000001"
FROZEN_CLAIM_ID = "f1000000000040008000000000000011"
FROZEN_PROPOSED_EVENT_ID = "f1000000000040008000000000000012"
FROZEN_GESTURE_ID = "f1000000000040008000000000000013"
FROZEN_CONFIRMED_EVENT_ID = "f1000000000040008000000000000014"
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


def _durable_history(path: Path) -> dict[str, object]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        store_info = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT store_id, profile, schema_version, title FROM store_info"
            )
        )
        claims = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT id, proposition, canonical_sha256, claim_kind, created_at "
                "FROM claims ORDER BY id"
            )
        )
        statuses = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT id, claim_id, status, actor_kind, actor_ref, basis_kind, "
                "basis_ref, at FROM claim_status_events ORDER BY seq"
            )
        )
        ledger = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT record_type, record_key FROM ledger_records ORDER BY seq"
            )
        )
        migration_history = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT version, description, code_hash, hash_format "
                "FROM _migration_history ORDER BY version"
            )
        )
    finally:
        conn.close()
    return {
        "store_info": store_info,
        "claims": claims,
        "statuses": statuses,
        "ledger": ledger,
        "migration_history": migration_history,
    }


def _restore_checked_in_frozen_v1(
    tmp_path: Path,
) -> tuple[Path, dict[str, object]]:
    manifest = json.loads((FROZEN_V1_DIR / "manifest.json").read_text(encoding="utf-8"))
    sql_dump = (FROZEN_V1_DIR / "store.db.sql").read_text(encoding="utf-8")
    normalized_text = sql_dump.replace("\r\n", "\n").rstrip("\n") + "\n"
    normalized_dump = normalized_text.encode("utf-8")
    assert hashlib.sha256(normalized_dump).hexdigest() == manifest["sql_sha256"]
    assert manifest["artifact_format"] == "sqlite3-sql-dump"
    assert manifest["schema_version"] == 1
    assert manifest["store_id"] == FROZEN_STORE_ID

    root = tmp_path / "checked-in-frozen-v1"
    sidecar = root / ".wb-truth"
    sidecar.mkdir(parents=True)
    conn = sqlite3.connect(str(sidecar / "store.db"))
    try:
        conn.executescript(normalized_text)
    finally:
        conn.close()
    (sidecar / "store.yaml").write_bytes((FROZEN_V1_DIR / "store.yaml").read_bytes())
    return root, manifest


@pytest.mark.parametrize(
    "fixture_path",
    FIXTURE_PATHS,
    ids=lambda path: path.stem,
)
def test_each_workload_runs_through_the_joined_truth_engine_surface(
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
def test_checked_in_frozen_v1_store_migrates_with_history_and_workload_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_path: Path,
) -> None:
    fixture = load_workload(fixture_path)
    root, manifest = _restore_checked_in_frozen_v1(tmp_path)
    database = root / ".wb-truth" / "store.db"
    assert _database_version(database) == 1
    frozen_history = _durable_history(database)
    assert frozen_history["store_info"] == (
        (
            FROZEN_STORE_ID,
            "project-canon",
            1,
            "Checked-in frozen truth schema v1",
        ),
    )
    assert [row[0] for row in frozen_history["claims"]] == [FROZEN_CLAIM_ID]
    assert [row[0] for row in frozen_history["statuses"]] == [
        FROZEN_PROPOSED_EVENT_ID,
        FROZEN_CONFIRMED_EVENT_ID,
    ]
    assert [row[2] for row in frozen_history["statuses"]] == [
        "proposed",
        "confirmed",
    ]
    assert frozen_history["ledger"] == tuple(
        tuple(row) for row in manifest["ledger_records"]
    )
    assert manifest["claim_id"] == FROZEN_CLAIM_ID
    assert manifest["status_event_ids"] == [
        FROZEN_PROPOSED_EVENT_ID,
        FROZEN_CONFIRMED_EVENT_ID,
    ]
    assert manifest["gesture_id"] == FROZEN_GESTURE_ID
    assert truth_uri(FROZEN_STORE_ID, "claim", FROZEN_CLAIM_ID) == (
        "wb-truth://f1000000000040008000000000000001/claim/"
        "f1000000000040008000000000000011"
    )

    migration_runner_type = truth_migrations._TruthMigrationRunner
    apply_migration = migration_runner_type._apply_one_locked

    def refuse_v1_regeneration(self, conn, migration):
        if migration.version == 1:
            raise AssertionError("checked-in v1 fixture invoked the current v1 DDL")
        return apply_migration(self, conn, migration)

    monkeypatch.setattr(truth_migrations, "TRUTH_MIGRATIONS", _v2_runner())
    monkeypatch.setattr(truth_export, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        migration_runner_type,
        "_apply_one_locked",
        refuse_v1_regeneration,
    )
    upgraded = TruthStore.open(root)
    monkeypatch.setattr(
        migration_runner_type,
        "_apply_one_locked",
        apply_migration,
    )

    snapshot = upgraded.paths.db.with_name("store.pre-v1.db")
    assert snapshot.is_file()
    assert _database_version(snapshot) == 1
    assert _database_version(upgraded.paths.db) == 2
    assert _durable_history(snapshot) == frozen_history

    upgraded_history = _durable_history(upgraded.paths.db)
    assert upgraded_history["claims"] == frozen_history["claims"]
    assert upgraded_history["statuses"] == frozen_history["statuses"]
    assert upgraded_history["ledger"] == frozen_history["ledger"]
    assert (
        upgraded_history["migration_history"][:1] == frozen_history["migration_history"]
    )
    assert [row[0] for row in upgraded_history["migration_history"]] == [1, 2]
    assert upgraded.store_id == FROZEN_STORE_ID
    assert upgraded.get_claim(FROZEN_CLAIM_ID) is not None
    assert b'"schema_version":2' in upgraded.paths.claims_export.read_bytes()
    with upgraded.connect() as conn:
        marker = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'migration_v2_marker'"
        ).fetchone()
    assert marker is not None

    # Profiles constrain new writes only. Exercise each declarative workload
    # against this same released-store artifact by adopting that workload's
    # mutable policy while retaining the frozen store's permanent identity.
    workload_profile = json.loads(json.dumps(fixture["profile"]))
    workload_profile.update(
        {
            "store_id": FROZEN_STORE_ID,
            "profile": "project-canon",
            "title": "Checked-in frozen truth schema v1",
        }
    )
    dump_profile(workload_profile, upgraded.paths.config)
    upgraded = TruthStore.open(root)
    assert upgraded.store_id == FROZEN_STORE_ID

    result = WorkloadRunner(upgraded, fixture).run()

    assert result.restored_store.store_id == FROZEN_STORE_ID
    assert _database_version(result.restored_store.paths.db) == 2
    for surviving_store in (upgraded, result.restored_store):
        seed = surviving_store.get_claim(FROZEN_CLAIM_ID)
        assert seed is not None
        assert (
            seed.proposition
            == "Frozen v1 identity and history survive every migration."
        )
        history = _durable_history(surviving_store.paths.db)
        assert history["statuses"][:2] == frozen_history["statuses"]
        assert history["ledger"][:4] == frozen_history["ledger"]


def test_real_export_hook_runs_once_after_commit_and_never_after_rollback(
    tmp_path: Path,
) -> None:
    fixture = load_workload(FIXTURE_DIR / "electricrag_supersession.yaml")
    root = tmp_path / "post-commit-export"
    root.mkdir()
    calls: list[str] = []

    def observe_commit(store: TruthStore) -> None:
        calls.append(store.store_id)

    store = TruthStore.create(
        root,
        fixture["profile"],
        on_commit=observe_commit,
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
