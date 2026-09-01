from __future__ import annotations

import hashlib
import sqlite3

import pytest

from work_buddy.journal_capture.migrations import (
    JOURNAL_MIGRATIONS,
    JournalMigrationRunner,
    _m001_bootstrap_historical_v7,
)
from work_buddy.journal_capture.models import JournalCaptureError
from work_buddy.journal_capture.authority import (
    JournalAuthorityCoordinator,
    JournalAuthorityStateError,
)
from work_buddy.journal_capture.store import JournalCaptureStore


def _schema_version(path) -> tuple[int, int]:
    with sqlite3.connect(path) as conn:
        return (
            int(conn.execute("PRAGMA user_version").fetchone()[0]),
            int(
                conn.execute(
                    "SELECT value FROM journal_meta WHERE key='schema_version'"
                ).fetchone()[0]
            ),
        )


def test_fresh_store_runs_real_migration_ladder_and_seeds_removable_profile(tmp_path):
    path = tmp_path / "journal.db"
    JournalCaptureStore(path)

    assert _schema_version(path) == (
        JOURNAL_MIGRATIONS.target_version,
        JOURNAL_MIGRATIONS.target_version,
    )
    with sqlite3.connect(path) as conn:
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM _migration_history ORDER BY version"
            )
        ]
        assert versions == list(range(1, JOURNAL_MIGRATIONS.target_version + 1))
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_profile_module_slots "
            "WHERE profile_id='simple-journal' AND required=1"
        ).fetchone()[0] == 0
        assert {
            row[1]
            for row in conn.execute("PRAGMA table_info(journal_field_value_revisions)")
        } >= {"authorship", "review_state"}
        assert {
            row[0]
            for row in conn.execute(
                "SELECT module_type_id FROM journal_module_type_revisions"
            )
        } >= {"capture", "day_stream", "record_collection", "field_group"}


def test_adopts_inline_v7_and_bridges_entries_without_copying_prose(tmp_path):
    path = tmp_path / "legacy.db"
    exact = "private legacy text"
    with sqlite3.connect(path) as conn:
        _m001_bootstrap_historical_v7(conn)
        conn.execute(
            """
            INSERT INTO journal_captures(
                capture_id,client_mutation_id,request_sha256,source_ref,
                representation_id,submission_id,command_id,source_effect_id,
                day_id,requested_target,resolved_target,mode,input_mode,submitted_at,
                processing_status,entry_id,created_at,updated_at
            ) VALUES(
                'c1','m1','hash','wb-source://test/item','r1','s1','cmd1','fx1',
                'journal-day:2026-08-20:America/New_York:05:00','log','log','dumb',
                'import','2026-08-20T12:00:00+00:00',
                'not_requested','e1','2026-08-20T12:00:00+00:00',
                '2026-08-20T12:00:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO journal_entries(
                entry_id,capture_id,day_id,entry_kind,source_ref,content_sha256,
                markdown,created_at,updated_at,processing_status,projection_marker
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "e1",
                "c1",
                "journal-day:2026-08-20:America/New_York:05:00",
                "log",
                "wb-source://test/item",
                hashlib.sha256(exact.encode()).hexdigest(),
                exact,
                "2026-08-20T12:00:00+00:00",
                "2026-08-20T12:00:00+00:00",
                "not_requested",
                "marker-e1",
            ),
        )
        conn.execute(
            "UPDATE journal_meta SET value='7' WHERE key='schema_version'"
        )
        conn.execute("PRAGMA user_version=0")

    JournalCaptureStore(path)

    assert _schema_version(path) == (
        JOURNAL_MIGRATIONS.target_version,
        JOURNAL_MIGRATIONS.target_version,
    )
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        bridge = conn.execute(
            "SELECT * FROM journal_items WHERE item_id='e1'"
        ).fetchone()
        assert bridge["authority_kind"] == "legacy_entry"
        assert bridge["legacy_entry_id"] == "e1"
        assert bridge["local_date"] == "2026-08-20"
        assert bridge["current_plain_value"] is None
        assert bridge["current_content_sha256"] is None
        assert conn.execute(
            "SELECT markdown FROM journal_entries WHERE entry_id='e1'"
        ).fetchone()[0] == exact
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_search_outbox "
            "WHERE aggregate_type='item' AND aggregate_id='e1'"
        ).fetchone()[0] == 1


def test_read_only_store_accepts_inline_v7_without_migrating_it(tmp_path):
    path = tmp_path / "legacy-read-only.db"
    with sqlite3.connect(path) as conn:
        _m001_bootstrap_historical_v7(conn)
        conn.execute("UPDATE journal_meta SET value='7' WHERE key='schema_version'")
        conn.execute("PRAGMA user_version=0")
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    store = JournalCaptureStore(path, read_only=True)

    assert store.read_only is True
    assert store.list_captures("2026-08-20") == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert _schema_version(path) == (0, 7)
    assert not path.with_name(path.name + "-wal").exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='_migration_history'"
        ).fetchone()[0] == 0


def test_future_user_version_is_rejected_without_touching_database(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE future_table(value TEXT)")
        conn.execute("INSERT INTO future_table VALUES('keep')")
        conn.execute("PRAGMA user_version=999")
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(JournalCaptureError, match="unsupported_journal_capture_schema"):
        JournalCaptureStore(path)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not path.with_name(path.name + "-wal").exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM future_table").fetchone()[0] == "keep"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='_migration_history'"
        ).fetchone()[0] == 0


def test_native_revision_tables_reject_update_and_delete(tmp_path):
    path = tmp_path / "journal.db"
    JournalCaptureStore(path)
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="is_immutable"):
            conn.execute(
                "UPDATE journal_profile_revisions SET name='changed' "
                "WHERE profile_id='simple-journal'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="is_immutable"):
            conn.execute(
                "DELETE FROM journal_interaction_behavior_revisions "
                "WHERE behavior_id='human_value'"
            )


def test_native_ladder_adopts_v11_rows_and_adds_import_and_authority_ledgers(tmp_path):
    path = tmp_path / "native-v11.db"
    v11 = JournalMigrationRunner(
        "journal_capture", migrations=JOURNAL_MIGRATIONS.migrations[:11]
    )
    with sqlite3.connect(path) as conn:
        v11.run(conn)
        conn.execute(
            """
            INSERT INTO journal_items(
                item_id,local_date,item_kind,authority_kind,current_plain_value,
                current_content_sha256,interaction_behavior_id,
                interaction_behavior_version,privacy_class,search_mode,source_ref,
                created_at,updated_at
            ) VALUES(
                'pre-v12-item','2026-08-20','record','native_plain','kept','hash',
                'human_value',1,'private','lexical','wb-source://test/item/source',
                '2026-08-20T12:00:00+00:00','2026-08-20T12:00:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO journal_item_revisions(
                item_id,revision,authority_kind,plain_value,content_sha256,lifecycle,
                actor_json,source_ref,authorship,review_state,intent_id,created_at
            ) VALUES(
                'pre-v12-item',1,'native_plain','kept','hash','current','{}',
                'wb-source://test/item/source','human','reviewed','intent-1',
                '2026-08-20T12:00:00+00:00'
            )
            """
        )

    JournalCaptureStore(path)

    assert _schema_version(path) == (
        JOURNAL_MIGRATIONS.target_version,
        JOURNAL_MIGRATIONS.target_version,
    )
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT plain_value,review_state FROM journal_item_revisions "
            "WHERE item_id='pre-v12-item'"
        ).fetchone() == ("kept", "reviewed")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "journal_import_cohorts",
            "journal_import_files",
            "journal_import_spans",
            "journal_import_receipts",
            "journal_import_progress",
            "journal_import_state_transitions",
            "journal_authority_control",
            "journal_authority_transitions",
            "journal_native_capture_bindings",
            "journal_native_redactions",
            "journal_import_source_redactions",
            "journal_native_source_dependencies",
            "journal_native_source_redactions",
            "journal_field_source_dependencies",
            "journal_field_source_redactions",
            "journal_import_profile_mappings",
            "journal_import_field_mappings",
            "journal_import_typed_observations",
            "journal_cutover_gate",
            "journal_cutover_gate_transitions",
        } <= tables
        assert conn.execute(
            "SELECT state,gate_revision FROM journal_cutover_gate WHERE singleton=1"
        ).fetchone() == ("open", 1)
        assert {
            row[1] for row in conn.execute("PRAGMA table_info(journal_field_values)")
        } >= {"import_cohort_id"}
        assert {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(journal_profile_activation_epochs)"
            )
        } >= {"import_cohort_id"}
        assert {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(journal_day_composition_snapshots)"
            )
        } >= {"import_cohort_id"}
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_interaction_behavior_revisions "
            "WHERE behavior_id='provenance_only' AND behavior_version=1"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="is_immutable"):
            conn.execute(
                "UPDATE journal_item_revisions SET review_state='unknown' "
                "WHERE item_id='pre-v12-item'"
            )


def test_v18_adopts_typed_value_history_with_honest_unknown_provenance(tmp_path):
    path = tmp_path / "native-v17-field-history.db"
    v17 = JournalMigrationRunner(
        "journal_capture", migrations=JOURNAL_MIGRATIONS.migrations[:17]
    )
    at = "2026-08-20T12:00:00+00:00"
    with sqlite3.connect(path) as conn:
        v17.run(conn)
        conn.execute(
            "INSERT INTO journal_field_definition_versions("
            "field_id,definition_version,owner,stable_key,label,value_kind,"
            "value_codec_version,behavior_id,behavior_version,privacy_class,"
            "search_mode,disclosure_policy_id,definition_sha256,created_at) "
            "VALUES('readiness',1,'test','readiness','Readiness','scale',1,"
            "'human_value',1,'private','structured_only','private','hash',?)",
            (at,),
        )
        conn.execute(
            "INSERT INTO journal_field_values("
            "value_id,local_date,composition_slot_id,module_instance_id,"
            "module_instance_version,field_id,field_definition_version,"
            "value_codec_version,value_kind,number_value,source_ref,authorship,"
            "review_state,ingested_at,current_revision,updated_at) "
            "VALUES('readiness:2026-08-20','2026-08-20','field:readiness','daily',1,"
            "'readiness',1,1,'scale',4,'wb-source://test/item/source','human',"
            "'reviewed',?,1,?)",
            (at, at),
        )
        conn.execute(
            "INSERT INTO journal_field_value_revisions("
            "value_id,revision,value_json,value_sha256,actor_json,source_ref,"
            "intent_id,created_at) VALUES('readiness:2026-08-20',1,'4','hash','{}',"
            "'wb-source://test/item/source','intent-1',?)",
            (at,),
        )

    JournalCaptureStore(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT value_json,authorship,review_state "
            "FROM journal_field_value_revisions"
        ).fetchone() == ("4", "unknown", "unknown")
        assert {
            row[1] for row in conn.execute("PRAGMA table_info(journal_field_values)")
        } >= {"import_cohort_id"}
        with pytest.raises(sqlite3.IntegrityError, match="is_immutable"):
            conn.execute(
                "UPDATE journal_field_value_revisions SET authorship='human' "
                "WHERE value_id='readiness:2026-08-20' AND revision=1"
            )


def test_v15_fences_preexisting_database_only_import_without_source_dependency(
    tmp_path,
):
    path = tmp_path / "native-v14-active-import.db"
    v14 = JournalMigrationRunner(
        "journal_capture", migrations=JOURNAL_MIGRATIONS.migrations[:14]
    )
    at = "2026-08-20T12:00:00+00:00"
    with sqlite3.connect(path) as conn:
        v14.run(conn)
        conn.execute(
            "INSERT INTO journal_import_cohorts("
            "cohort_id,client_mutation_id,request_sha256,inventory_sha256,"
            "parser_version,mapping_version,mapping_sha256,parse_report_sha256,"
            "state,state_revision,expected_file_count,expected_byte_count,"
            "expected_span_count,expected_item_count,actor_json,created_at,updated_at,"
            "verified_at,sealed_at,seal_sha256) "
            "VALUES('old-cohort','old-mutation','r','i','p','m','ms','prs','sealed',"
            "4,1,1,0,0,'{}',?,?,?,?,?)",
            (at, at, at, at, "seal"),
        )
        conn.execute(
            "INSERT INTO journal_import_files("
            "cohort_id,file_id,relative_path,local_date,byte_length,mtime_ns,"
            "raw_sha256,encoding,newline,expected_parse_sha256,expected_span_count,"
            "state,ingress_client_mutation_id,stage_request_sha256,source_ref,"
            "representation_id,submission_id,staged_at) "
            "VALUES('old-cohort','old-file','2026-08-20.md','2026-08-20',1,1,"
            "'raw','utf-8','lf','parse',0,'staged','ingress-old','stage-old',"
            "'wb-source://old/item','old-representation','old-submission',?)",
            (at,),
        )
        conn.execute(
            "UPDATE journal_authority_control SET mode='database_only',"
            "activated_cohort_id='old-cohort' WHERE singleton=1"
        )
        conn.execute(
            "UPDATE journal_domain_state SET value='database_only' "
            "WHERE key='content_authority'"
        )

    store = JournalCaptureStore(path)
    state = JournalAuthorityCoordinator(store).state()

    assert state.mode == "recovery_fenced"
    assert state.prior_mode == "database_only"
    assert state.fence_code == "pre_v15_import_source_dependencies_missing"
    with pytest.raises(JournalAuthorityStateError, match="dependency reconciliation"):
        JournalAuthorityCoordinator(store).recover(
            client_mutation_id="unsafe-recover-0001",
            actor={"kind": "migration_operator", "id": "test"},
        )
