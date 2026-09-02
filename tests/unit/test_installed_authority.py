from __future__ import annotations

import sqlite3
import shutil
import tarfile
from pathlib import Path
from typing import Callable

import pytest

from work_buddy.installed_authority import (
    InstalledAuthorityError,
    confirm_domain_seal,
    initialize_installed_authority_ledger,
    inspect_restore_rebind_plan,
    installed_authority_status,
    ledger_path_for,
    prepare_domain_seal,
    recover_incomplete_domain_seal,
    rebind_restored_authority_paths,
    require_domain_store_open,
)


COHORT = "installed-seal-test-cohort"
INVENTORY = "a" * 64


def test_preflight_initializer_creates_nonzero_empty_valid_ledger(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "db" / "journal_capture.db"

    ledger = initialize_installed_authority_ledger(authority)
    first_size = ledger.stat().st_size
    replay = initialize_installed_authority_ledger(authority)

    assert replay == ledger == tmp_path / "db" / "installed_authority.db"
    assert first_size > 0
    assert replay.stat().st_size == first_size
    conn = sqlite3.connect(ledger)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert [tuple(row) for row in conn.execute("PRAGMA quick_check")] == [
            ("ok",)
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM installed_domain_authority"
        ).fetchone()[0] == 0
    finally:
        conn.close()
    assert installed_authority_status("journal", authority) is None


def test_preflight_initializer_rejects_existing_authority_without_modifying_it(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "db" / "projects.db"
    ledger = initialize_installed_authority_ledger(authority)
    conn = sqlite3.connect(ledger)
    try:
        conn.execute(
            "INSERT INTO installed_domain_authority("
            "domain,state,cohort_id,authority_db_path_sha256,revision,"
            "sealing_started_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("projects", "sealing", COHORT, "f" * 64, 1, "now", "now"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(InstalledAuthorityError, match="not empty"):
        initialize_installed_authority_ledger(authority)

    conn = sqlite3.connect(ledger)
    try:
        assert conn.execute(
            "SELECT domain,state,cohort_id FROM installed_domain_authority"
        ).fetchall() == [("projects", "sealing", COHORT)]
    finally:
        conn.close()


def _publish_minimal_sealed_authority(
    domain: str,
    path: Path,
    *,
    confirm: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        if domain == "journal":
            conn.executescript(
                """
                CREATE TABLE journal_authority_control (
                    singleton INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL,
                    activated_cohort_id TEXT
                );
                CREATE TABLE journal_import_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO journal_authority_control VALUES (1,'database_only',?)",
                (COHORT,),
            )
            conn.execute(
                "INSERT INTO journal_import_cohorts VALUES (?,'sealed')", (COHORT,)
            )
        elif domain == "projects":
            conn.executescript(
                """
                CREATE TABLE project_authority_state (
                    singleton INTEGER PRIMARY KEY,
                    authority TEXT NOT NULL,
                    state TEXT NOT NULL,
                    sealed_cohort_id TEXT
                );
                CREATE TABLE project_import_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO project_authority_state VALUES (1,'sqlite','active',?)",
                (COHORT,),
            )
            conn.execute(
                "INSERT INTO project_import_cohorts VALUES (?,'sealed')", (COHORT,)
            )
        elif domain == "contracts":
            conn.executescript(
                """
                CREATE TABLE contract_authority (
                    singleton INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    sealed_cohort_id TEXT
                );
                CREATE TABLE contract_import_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO contract_authority VALUES (1,'native',?)", (COHORT,)
            )
            conn.execute(
                "INSERT INTO contract_import_cohorts VALUES (?,'sealed')", (COHORT,)
            )
        else:
            conn.executescript(
                """
                CREATE TABLE personal_knowledge_authority (
                    singleton INTEGER PRIMARY KEY,
                    authority TEXT NOT NULL,
                    sealed_cohort_id TEXT
                );
                CREATE TABLE personal_import_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO personal_knowledge_authority VALUES (1,'sqlite',?)",
                (COHORT,),
            )
            conn.execute(
                "INSERT INTO personal_import_cohorts VALUES (?,'sealed')", (COHORT,)
            )
        conn.commit()
    finally:
        conn.close()
    prepare_domain_seal(domain, path, cohort_id=COHORT)
    if confirm:
        status = confirm_domain_seal(domain, path, cohort_id=COHORT)
        assert status.state == "sealed"


def _publish_minimal_preseal_authority(domain: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE cutover_maintenance (
                singleton INTEGER PRIMARY KEY,
                domain TEXT NOT NULL,
                state TEXT NOT NULL,
                cohort_id TEXT,
                inventory_sha256 TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO cutover_maintenance VALUES (1,?,'preseal_fenced',?,?)",
            (domain, COHORT, INVENTORY),
        )
        if domain == "journal":
            conn.executescript(
                """
                CREATE TABLE journal_authority_control (
                    singleton INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL,
                    activated_cohort_id TEXT
                );
                CREATE TABLE journal_cutover_gate (
                    singleton INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    cohort_id TEXT
                );
                CREATE TABLE journal_import_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    inventory_sha256 TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO journal_authority_control VALUES "
                "(1,'legacy_compatibility',NULL)"
            )
            conn.execute(
                "INSERT INTO journal_cutover_gate VALUES (1,'paused',?)", (COHORT,)
            )
            conn.execute(
                "INSERT INTO journal_import_cohorts VALUES (?,'sealed',?)",
                (COHORT, INVENTORY),
            )
        elif domain == "projects":
            conn.executescript(
                """
                CREATE TABLE project_authority_state (
                    singleton INTEGER PRIMARY KEY,
                    authority TEXT NOT NULL,
                    state TEXT NOT NULL,
                    sealed_cohort_id TEXT
                );
                CREATE TABLE project_import_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    inventory_sha256 TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO project_authority_state VALUES "
                "(1,'legacy_markdown','write_fenced',NULL)"
            )
            conn.execute(
                "INSERT INTO project_import_cohorts VALUES (?,'verified',?)",
                (COHORT, INVENTORY),
            )
        elif domain == "contracts":
            conn.executescript(
                """
                CREATE TABLE contract_authority (
                    singleton INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    sealed_cohort_id TEXT
                );
                CREATE TABLE contract_import_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    inventory_sha256 TEXT NOT NULL
                );
                """
            )
            conn.execute("INSERT INTO contract_authority VALUES (1,'legacy',NULL)")
            conn.execute(
                "INSERT INTO contract_import_cohorts VALUES (?,'staged',?)",
                (COHORT, INVENTORY),
            )
        else:
            conn.executescript(
                """
                CREATE TABLE personal_knowledge_authority (
                    singleton INTEGER PRIMARY KEY,
                    authority TEXT NOT NULL,
                    sealed_cohort_id TEXT
                );
                CREATE TABLE personal_import_cohorts (
                    cohort_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    inventory_sha256 TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO personal_knowledge_authority VALUES "
                "(1,'legacy_markdown',NULL)"
            )
            conn.execute(
                "INSERT INTO personal_import_cohorts VALUES (?,'verified',?)",
                (COHORT, INVENTORY),
            )
        conn.commit()
    finally:
        conn.close()
    prepare_domain_seal(domain, path, cohort_id=COHORT)


def _commit_minimal_native_authority(domain: str, path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        if domain == "journal":
            conn.execute(
                "UPDATE journal_authority_control SET mode='database_only',"
                "activated_cohort_id=? WHERE singleton=1",
                (COHORT,),
            )
        elif domain == "projects":
            conn.execute(
                "UPDATE project_authority_state SET authority='sqlite',state='active',"
                "sealed_cohort_id=? WHERE singleton=1",
                (COHORT,),
            )
            conn.execute(
                "UPDATE project_import_cohorts SET state='sealed' WHERE cohort_id=?",
                (COHORT,),
            )
        elif domain == "contracts":
            conn.execute(
                "UPDATE contract_authority SET state='native',sealed_cohort_id=? "
                "WHERE singleton=1",
                (COHORT,),
            )
            conn.execute(
                "UPDATE contract_import_cohorts SET state='sealed' WHERE cohort_id=?",
                (COHORT,),
            )
        else:
            conn.execute(
                "UPDATE personal_knowledge_authority SET authority='sqlite',"
                "sealed_cohort_id=? WHERE singleton=1",
                (COHORT,),
            )
            conn.execute(
                "UPDATE personal_import_cohorts SET state='sealed' WHERE cohort_id=?",
                (COHORT,),
            )
        conn.commit()
    finally:
        conn.close()


def _break_database(path: Path, failure: str) -> None:
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"not-a-sqlite-database")


@pytest.mark.parametrize(
    "domain, filename",
    [
        ("journal", "journal_capture.db"),
        ("projects", "projects.db"),
        ("contracts", "contracts.db"),
        ("personal_knowledge", "personal_knowledge.db"),
    ],
)
def test_precommit_seal_crash_is_fail_closed_but_exact_roll_forward_can_resume(
    tmp_path: Path,
    domain: str,
    filename: str,
) -> None:
    database = tmp_path / filename
    _publish_minimal_preseal_authority(domain, database)

    with pytest.raises(InstalledAuthorityError, match="recovery is required"):
        require_domain_store_open(domain, database)
    with pytest.raises(InstalledAuthorityError, match="does not match"):
        with recover_incomplete_domain_seal(
            domain,
            database,
            cohort_id=COHORT,
            inventory_sha256="b" * 64,
        ):
            pass

    with recover_incomplete_domain_seal(
        domain,
        database,
        cohort_id=COHORT,
        inventory_sha256=INVENTORY,
    ) as recovery:
        assert recovery == "resumed"
        assert require_domain_store_open(domain, database) is None
        _commit_minimal_native_authority(domain, database)
        confirmed = confirm_domain_seal(domain, database, cohort_id=COHORT)
        assert confirmed.state == "sealed"
        reopened = require_domain_store_open(domain, database)
        assert reopened is not None and reopened.cohort_id == COHORT

    status = installed_authority_status(domain, database)
    assert status is not None
    assert status.state == "sealed"
    assert status.cohort_id == COHORT


@pytest.mark.parametrize(
    "domain, filename",
    [
        ("journal", "journal_capture.db"),
        ("projects", "projects.db"),
        ("contracts", "contracts.db"),
        ("personal_knowledge", "personal_knowledge.db"),
    ],
)
def test_postcommit_seal_crash_is_confirmed_without_opening_legacy_authority(
    tmp_path: Path,
    domain: str,
    filename: str,
) -> None:
    database = tmp_path / filename
    _publish_minimal_sealed_authority(domain, database, confirm=False)

    with pytest.raises(InstalledAuthorityError, match="recovery is required"):
        require_domain_store_open(domain, database)
    with recover_incomplete_domain_seal(
        domain,
        database,
        cohort_id=COHORT,
        inventory_sha256=INVENTORY,
    ) as recovery:
        assert recovery == "confirmed"

    status = installed_authority_status(domain, database)
    assert status is not None
    assert status.state == "sealed"


def test_roll_forward_recovery_crash_keeps_the_incomplete_latch_fail_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "projects.db"
    _publish_minimal_preseal_authority("projects", database)

    with pytest.raises(RuntimeError, match="simulated second crash"):
        with recover_incomplete_domain_seal(
            "projects",
            database,
            cohort_id=COHORT,
            inventory_sha256=INVENTORY,
        ):
            raise RuntimeError("simulated second crash")

    with pytest.raises(InstalledAuthorityError, match="recovery is required"):
        require_domain_store_open("projects", database)


@pytest.mark.parametrize(
    "domain, filename",
    [
        ("journal", "journal_capture.db"),
        ("projects", "projects.db"),
        ("contracts", "contracts.db"),
        ("personal_knowledge", "personal_knowledge.db"),
    ],
)
@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_installed_seal_fails_closed_when_bound_database_is_unavailable(
    tmp_path: Path,
    domain: str,
    filename: str,
    failure: str,
) -> None:
    database = tmp_path / filename
    _publish_minimal_sealed_authority(domain, database)
    _break_database(database, failure)

    with pytest.raises(InstalledAuthorityError):
        installed_authority_status(domain, database)
    assert database.exists() is (failure == "corrupt")


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
@pytest.mark.parametrize(
    "operation",
    [
        lambda ops: ops.journal_state(target="today"),
        lambda ops: ops.journal_state(target="today", create_on_read=True),
        lambda ops: ops.running_notes(same_day=True),
        lambda ops: ops.journal_sign_in(target="today"),
        lambda ops: ops.journal_sign_in(
            target="today", write_fields={"rating": 4}
        ),
        lambda ops: ops.day_planner(action="status"),
        lambda ops: ops.day_planner(action="write", focused_tasks=[]),
        lambda ops: ops.journal_write(
            mode="log_entries", entries=[["09:00", "test"]]
        ),
    ],
    ids=[
        "state-read",
        "state-create-on-read",
        "running-notes-read",
        "sign-in-read",
        "sign-in-write",
        "planner-read",
        "planner-write",
        "journal-write",
    ],
)
def test_every_native_journal_adapter_rejects_missing_or_corrupt_sealed_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    operation: Callable,
) -> None:
    from work_buddy.journal_capture import native_ops
    from work_buddy.journal_capture.store import JournalCaptureStore

    database = tmp_path / "journal_capture.db"
    _publish_minimal_sealed_authority("journal", database)
    _break_database(database, failure)

    def runtime():
        store = JournalCaptureStore(database)
        return object(), store, object()

    monkeypatch.setattr(
        native_ops,
        "_read_runtime",
        lambda: (JournalCaptureStore(database, read_only=True), object()),
    )
    monkeypatch.setattr(native_ops, "_write_runtime", runtime)
    monkeypatch.setattr(native_ops, "_legacy_allowed", lambda: True)

    with pytest.raises(InstalledAuthorityError):
        operation(native_ops)
    assert database.exists() is (failure == "corrupt")


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_projects_read_and_mutation_adapters_never_resume_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from work_buddy.projects import authority, store

    database = tmp_path / "projects.db"
    _publish_minimal_sealed_authority("projects", database)
    _break_database(database, failure)
    monkeypatch.setattr(store, "_db_path", lambda: database)

    for operation in (
        authority.authority_status,
        lambda: authority.update_project_authoritatively("alpha", {"name": "Alpha"}),
    ):
        with pytest.raises(InstalledAuthorityError):
            operation()
        assert database.exists() is (failure == "corrupt")


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_contract_read_and_mutation_adapters_never_resume_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from work_buddy import contracts
    from work_buddy.contracts_domain import ops, provider

    database = tmp_path / "contracts.db"
    _publish_minimal_sealed_authority("contracts", database)
    _break_database(database, failure)
    monkeypatch.setattr(provider, "default_db_path", lambda: database)
    monkeypatch.setattr(ops, "get_originating_session", lambda: "session-test")

    operations = (
        contracts.load_all_contracts,
        lambda: ops.create_contract(
            payload={"title": "Blocked"}, client_mutation_id="blocked-create"
        ),
    )
    for operation in operations:
        with pytest.raises(InstalledAuthorityError):
            operation()
        assert database.exists() is (failure == "corrupt")


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_personal_read_and_mint_adapters_never_resume_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from work_buddy.knowledge import store as knowledge_store
    from work_buddy.knowledge import vault_editor
    from work_buddy.knowledge.personal import provider, store

    database = tmp_path / "personal_knowledge.db"
    _publish_minimal_sealed_authority("personal_knowledge", database)
    _break_database(database, failure)
    monkeypatch.setattr(store, "resolve", lambda _resource: database)
    provider.set_personal_knowledge_provider(None)
    monkeypatch.setattr(knowledge_store, "_VAULT_STORE", {})

    operations = (
        lambda: provider.get_personal_knowledge_provider().load_units(),
        lambda: knowledge_store.load_vault(),
        lambda: vault_editor.mint_personal_unit(name="Blocked", category="test"),
    )
    for operation in operations:
        provider.set_personal_knowledge_provider(
            provider.LegacyMarkdownPersonalKnowledgeProvider()
        )
        with pytest.raises(InstalledAuthorityError):
            operation()
        assert database.exists() is (failure == "corrupt")
    provider.set_personal_knowledge_provider(None)


def test_installed_authority_is_vital_and_round_trips_through_local_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from work_buddy.backups import local
    from work_buddy.backups.restore import (
        _apply_migrations_inplace,
        _current_known_max_schema_versions,
    )

    database = tmp_path / "live" / "projects.db"
    _publish_minimal_sealed_authority("projects", database)
    ledger = ledger_path_for(database)
    assert local.VITAL_DBS["installed_authority"] == "db/installed-authority"
    assert _current_known_max_schema_versions()["installed_authority"] == 1

    monkeypatch.setattr(
        local, "_resolve_vital_dbs", lambda: {"installed_authority": ledger}
    )
    monkeypatch.setattr(local, "data_dir", lambda name="": tmp_path / name)
    result = local.run_backup(manual=True)
    restore_dir = tmp_path / "restore"
    restore_dir.mkdir()
    with tarfile.open(result["tarball_path"], "r:gz") as archive:
        assert "installed_authority.db" in archive.getnames()
        archive.extract("installed_authority.db", path=restore_dir)
    restored = restore_dir / "installed_authority.db"
    _apply_migrations_inplace("installed_authority", restored)
    conn = sqlite3.connect(restored)
    try:
        row = conn.execute(
            "SELECT state,cohort_id FROM installed_domain_authority "
            "WHERE domain='projects'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("sealed", COHORT)


def test_restore_unions_live_and_snapshot_installed_seals(tmp_path: Path) -> None:
    from work_buddy.backups.restore import _merge_installed_authority_state

    live = tmp_path / "live"
    staging = tmp_path / "staging"
    _publish_minimal_sealed_authority("projects", live / "projects.db")
    _publish_minimal_sealed_authority("contracts", staging / "contracts.db")

    _merge_installed_authority_state(live, staging)

    conn = sqlite3.connect(staging / "installed_authority.db")
    try:
        rows = conn.execute(
            "SELECT domain,state FROM installed_domain_authority ORDER BY domain"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("contracts", "sealed"), ("projects", "sealed")]


@pytest.mark.parametrize(
    "domain,filename",
    [
        ("journal", "journal_capture.db"),
        ("projects", "projects.db"),
        ("contracts", "contracts.db"),
        ("personal_knowledge", "personal_knowledge.db"),
    ],
)
def test_relocated_restore_requires_exact_rebind_and_then_opens(
    tmp_path: Path,
    domain: str,
    filename: str,
) -> None:
    source_db = tmp_path / "source-machine" / "db" / filename
    _publish_minimal_sealed_authority(domain, source_db)
    source_ledger = ledger_path_for(source_db)
    target_root = tmp_path / "new-machine" / "db"
    target_root.mkdir(parents=True)
    target_db = target_root / filename
    target_ledger = target_root / "installed_authority.db"
    shutil.copy2(source_db, target_db)
    shutil.copy2(source_ledger, target_ledger)

    with pytest.raises(InstalledAuthorityError, match="path does not match"):
        installed_authority_status(domain, target_db)

    plan = inspect_restore_rebind_plan(
        target_ledger,
        {domain: target_db},
    )
    assert plan["required"] is True
    assert plan["ready"] is True
    assert plan["rows"][0]["blocker"] is None

    with pytest.raises(InstalledAuthorityError, match="plan changed"):
        rebind_restored_authority_paths(
            target_ledger,
            {domain: target_db},
            expected_plan_sha256="f" * 64,
            snapshot_id="snap-relocated",
        )
    with pytest.raises(InstalledAuthorityError, match="path does not match"):
        installed_authority_status(domain, target_db)

    receipt = rebind_restored_authority_paths(
        target_ledger,
        {domain: target_db},
        expected_plan_sha256=plan["plan_sha256"],
        snapshot_id="snap-relocated",
    )
    assert receipt["result"] == "rebound"
    assert receipt["rebound_domains"] == [domain]
    status = installed_authority_status(domain, target_db)
    assert status is not None
    assert status.cohort_id == COHORT
    assert status.revision == 3

    replay_plan = inspect_restore_rebind_plan(target_ledger, {domain: target_db})
    assert replay_plan["required"] is False
    replay = rebind_restored_authority_paths(
        target_ledger,
        {domain: target_db},
        expected_plan_sha256=replay_plan["plan_sha256"],
        snapshot_id="snap-relocated",
    )
    assert replay["result"] == "already_bound"
    assert replay["rebound_domains"] == []


def test_restore_rebind_is_all_or_nothing_when_any_target_cannot_prove_cohort(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source" / "db"
    target_root = tmp_path / "target" / "db"
    target_root.mkdir(parents=True)
    source_projects = source_root / "projects.db"
    source_contracts = source_root / "contracts.db"
    _publish_minimal_sealed_authority("projects", source_projects)
    _publish_minimal_sealed_authority("contracts", source_contracts)
    target_projects = target_root / "projects.db"
    target_contracts = target_root / "contracts.db"
    target_ledger = target_root / "installed_authority.db"
    shutil.copy2(source_projects, target_projects)
    shutil.copy2(source_contracts, target_contracts)
    shutil.copy2(ledger_path_for(source_projects), target_ledger)
    target_contracts.unlink()
    targets = {
        "projects": target_projects,
        "contracts": target_contracts,
    }

    plan = inspect_restore_rebind_plan(target_ledger, targets)
    assert plan["required"] is True
    assert plan["ready"] is False
    assert {
        row["domain"]: row["blocker"] for row in plan["rows"]
    } == {
        "contracts": "sealed_authority_unproven",
        "projects": None,
    }
    with pytest.raises(InstalledAuthorityError, match="every sealed cohort"):
        rebind_restored_authority_paths(
            target_ledger,
            targets,
            expected_plan_sha256=plan["plan_sha256"],
            snapshot_id="snap-incomplete",
        )
    with pytest.raises(InstalledAuthorityError, match="path does not match"):
        installed_authority_status("projects", target_projects)
