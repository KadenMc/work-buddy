from __future__ import annotations

import sqlite3

import pytest
from flask import Flask

from work_buddy.journal_capture import api as journal_api
from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.models import (
    JournalCaptureConflict,
    JournalCaptureValidationError,
    JournalValueDisposition,
)
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources.store import SourceStore


WINDOW = {
    "timezone": "America/New_York",
    "boundary": "04:00",
    "window_start": "2026-08-27T04:00:00-04:00",
    "window_end": "2026-08-28T04:00:00-04:00",
}


def _domain(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    return store, JournalDomainService(store)


def _field_profile(domain: JournalDomainService) -> None:
    assert domain.create_field_definition_version(
        field_id="rating",
        owner="test",
        stable_key="rating",
        label="Rating",
        value_kind="scale",
        constraints={"minimum": 1, "maximum": 5},
        search_mode="structured_only",
    ) == 1
    domain.create_module_instance_version(
        module_instance_id="reflection",
        module_type_id="field_group",
        module_type_version=1,
        label="Reflection",
        schedule_kind="weekdays",
        schedule={"weekdays": [3]},  # 2026-08-27 is Thursday (Monday=0)
        fields=[
            {
                "slot_id": "rating",
                "field_id": "rating",
                "field_definition_version": 1,
            }
        ],
    )
    domain.create_profile_revision(
        profile_id="custom",
        name="Custom",
        modules=[
            {
                "slot_id": "reflection",
                "module_instance_id": "reflection",
                "module_instance_version": 1,
            }
        ],
        created_by="test",
    )
    assert domain.activate_profile(
        profile_id="custom",
        profile_revision=1,
        effective_local_date="2026-08-27",
        expected_activation_revision=1,
        client_mutation_id="activate-custom",
        actor={"kind": "human", "subject": "test"},
    ) == 2


def _scheduled_prompt_profile(domain: JournalDomainService) -> None:
    assert domain.create_field_definition_version(
        field_id="scheduled-rating",
        owner="test",
        stable_key="scheduled-rating",
        label="Scheduled rating",
        value_kind="scale",
        constraints={"minimum": 1, "maximum": 5},
        search_mode="structured_only",
    ) == 1
    assert domain.create_prompt_definition_version(
        prompt_id="scheduled-rating.prompt",
        field_id="scheduled-rating",
        field_definition_version=1,
        wording="How is the scheduled rating?",
        requiredness="required",
        schedule_kind="weekdays",
        schedule={"weekdays": [3]},
    ) == 1
    domain.create_module_instance_version(
        module_instance_id="scheduled-reflection",
        module_type_id="field_group",
        module_type_version=1,
        label="Scheduled reflection",
        fields=[
            {
                "slot_id": "rating",
                "field_id": "scheduled-rating",
                "field_definition_version": 1,
                "prompt_id": "scheduled-rating.prompt",
                "prompt_version": 1,
            }
        ],
    )
    domain.create_profile_revision(
        profile_id="scheduled-profile",
        name="Scheduled profile",
        modules=[
            {
                "slot_id": "scheduled-reflection",
                "module_instance_id": "scheduled-reflection",
                "module_instance_version": 1,
            }
        ],
        created_by="test",
    )
    assert domain.activate_profile(
        profile_id="scheduled-profile",
        profile_revision=1,
        effective_local_date="2026-08-27",
        expected_activation_revision=1,
        client_mutation_id="activate-scheduled-profile",
        actor={"kind": "human", "subject": "test"},
    ) == 2


def test_definition_registries_are_versioned_and_function_links_are_explicit(tmp_path):
    store, domain = _domain(tmp_path)
    assert domain.create_interaction_behavior_version(
        behavior_id="assistable_value",
        definition={"aiContribution": "suggestion_only", "truthEligibility": "unsupported"},
    ) == 1
    assert domain.create_function_contract_version(
        function_id="rating-score",
        value_kind="scale",
        cardinality="single",
        definition={"aggregation": "mean"},
    ) == 1
    assert domain.create_module_type_version(
        module_type_id="custom-field-group",
        definition={"family": "field_group", "multiplicity": "multiple"},
    ) == 1
    assert domain.create_field_definition_version(
        field_id="function-rating",
        owner="test",
        stable_key="function-rating",
        label="Rating",
        value_kind="scale",
        function_id="rating-score",
        function_version=1,
        behavior_id="assistable_value",
        behavior_version=1,
    ) == 1

    with pytest.raises(JournalCaptureConflict):
        domain.create_function_contract_version(
            function_id="rating-score",
            value_kind="scale",
            cardinality="single",
            definition={"aggregation": "median"},
            expected_version=0,
        )
    with pytest.raises(JournalCaptureValidationError):
        domain.create_field_definition_version(
            field_id="incompatible",
            owner="test",
            stable_key="incompatible",
            label="Incompatible",
            value_kind="short_text",
            function_id="rating-score",
            function_version=1,
        )

    with sqlite3.connect(store.path) as conn:
        linked = conn.execute(
            "SELECT function_id,function_version,behavior_id,behavior_version "
            "FROM journal_field_definition_versions "
            "WHERE field_id='function-rating'"
        ).fetchone()
        assert linked == ("rating-score", 1, "assistable_value", 1)


def test_day_resolution_is_pure_and_snapshot_pins_profile_and_schedule(tmp_path):
    store, domain = _domain(tmp_path)
    _field_profile(domain)

    projected = domain.resolve_day(local_date="2026-08-27", **WINDOW)
    assert projected.persisted is False
    assert projected.profile.profile_id == "custom"
    assert projected.modules[0].semantic_membership == "included"
    assert projected.modules[0].schedule_evidence["weekday"] == 3
    assert len(projected.fields) == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal_days").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_day_composition_snapshots"
        ).fetchone()[0] == 0

    frozen = domain.ensure_day(
        local_date="2026-08-27",
        boundary_policy_revision="settings:1",
        created_by="test-lifecycle",
        **WINDOW,
    )
    assert frozen.persisted is True
    assert frozen.composition_digest == projected.composition_digest

    # A later profile activation for the same date cannot reinterpret the day
    # whose immutable composition was already frozen.
    assert domain.activate_profile(
        profile_id="simple-journal",
        profile_revision=1,
        effective_local_date="2026-08-27",
        expected_activation_revision=2,
        client_mutation_id="activate-simple-late",
        actor={"kind": "human", "subject": "test"},
    ) == 3
    reread = domain.resolve_day(local_date="2026-08-27", **WINDOW)
    assert reread.profile.profile_id == "custom"
    assert reread.composition_digest == frozen.composition_digest


def test_prompt_schedule_is_resolved_and_frozen_per_logical_day(tmp_path):
    store, domain = _domain(tmp_path)
    _scheduled_prompt_profile(domain)

    included = domain.resolve_day(local_date="2026-08-27", **WINDOW)
    assert included.fields[0].prompt_id == "scheduled-rating.prompt"
    assert included.fields[0].prompt_version == 1
    assert included.fields[0].prompt_requiredness == "required"
    included_frozen = domain.ensure_day(
        local_date="2026-08-27",
        boundary_policy_revision="settings:1",
        created_by="test-lifecycle",
        **WINDOW,
    )
    assert included_frozen.fields[0].prompt_id == "scheduled-rating.prompt"

    friday_window = {
        **WINDOW,
        "window_start": "2026-08-28T04:00:00-04:00",
        "window_end": "2026-08-29T04:00:00-04:00",
    }
    excluded = domain.resolve_day(local_date="2026-08-28", **friday_window)
    assert len(excluded.fields) == 1
    assert excluded.fields[0].prompt_id is None
    assert excluded.fields[0].prompt_version is None
    assert excluded.fields[0].prompt_wording is None
    assert excluded.fields[0].prompt_requiredness is None

    frozen = domain.ensure_day(
        local_date="2026-08-28",
        boundary_policy_revision="settings:1",
        created_by="test-lifecycle",
        **friday_window,
    )
    assert frozen.persisted is True
    assert frozen.fields[0].prompt_id is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT fields.prompt_id,fields.prompt_version "
            "FROM journal_day_composition_fields AS fields "
            "JOIN journal_day_composition_snapshots AS snapshot "
            "ON snapshot.snapshot_id=fields.snapshot_id "
            "JOIN journal_days AS day ON day.day_id=snapshot.day_id "
            "WHERE day.local_date='2026-08-28'"
        ).fetchone() == (None, None)

    assert domain.create_prompt_definition_version(
        prompt_id="scheduled-rating.prompt",
        field_id="scheduled-rating",
        field_definition_version=1,
        wording="How is the scheduled rating now?",
        requiredness="required",
        schedule_kind="always",
        expected_version=1,
    ) == 2
    domain.create_module_instance_version(
        module_instance_id="scheduled-reflection",
        module_type_id="field_group",
        module_type_version=1,
        label="Scheduled reflection",
        fields=[
            {
                "slot_id": "rating",
                "field_id": "scheduled-rating",
                "field_definition_version": 1,
                "prompt_id": "scheduled-rating.prompt",
                "prompt_version": 2,
            }
        ],
        expected_version=1,
    )
    domain.create_profile_revision(
        profile_id="scheduled-profile",
        name="Scheduled profile",
        modules=[
            {
                "slot_id": "scheduled-reflection",
                "module_instance_id": "scheduled-reflection",
                "module_instance_version": 2,
            }
        ],
        created_by="test",
        expected_revision=1,
    )
    assert domain.activate_profile(
        profile_id="scheduled-profile",
        profile_revision=2,
        effective_local_date="2026-08-28",
        expected_activation_revision=2,
        client_mutation_id="activate-always-prompt",
        actor={"kind": "human", "subject": "test"},
    ) == 3

    reread = domain.resolve_day(local_date="2026-08-28", **friday_window)
    assert reread.composition_digest == frozen.composition_digest
    assert reread.fields[0].prompt_id is None


def test_empty_profile_is_valid_and_all_default_modules_are_removable(tmp_path):
    _store, domain = _domain(tmp_path)
    profile = domain.create_profile_revision(
        profile_id="empty",
        name="Empty",
        modules=[],
        created_by="test",
    )
    assert profile.canonical_order == ()
    domain.activate_profile(
        profile_id="empty",
        profile_revision=1,
        effective_local_date="2026-08-27",
        expected_activation_revision=1,
        client_mutation_id="activate-empty",
        actor={"subject": "test"},
    )
    resolved = domain.resolve_day(local_date="2026-08-27", **WINDOW)
    assert resolved.modules == ()
    assert resolved.fields == ()


def test_typed_value_distinguishes_missing_from_zero_and_is_cas_idempotent(tmp_path):
    store, domain = _domain(tmp_path)
    _field_profile(domain)

    first = domain.put_field_value(
        value_id="value-1",
        local_date="2026-08-27",
        module_instance_id="reflection",
        module_instance_version=1,
        field_id="rating",
        field_definition_version=1,
        client_mutation_id="value-mutation-1",
        expected_revision=0,
        actor={"subject": "test"},
        value=3,
    )
    replay = domain.put_field_value(
        value_id="value-1",
        local_date="2026-08-27",
        module_instance_id="reflection",
        module_instance_version=1,
        field_id="rating",
        field_definition_version=1,
        client_mutation_id="value-mutation-1",
        expected_revision=0,
        actor={"subject": "test"},
        value=3,
    )
    assert replay == first

    missing = domain.put_field_value(
        value_id="value-1",
        local_date="2026-08-27",
        module_instance_id="reflection",
        module_instance_version=1,
        field_id="rating",
        field_definition_version=1,
        client_mutation_id="value-mutation-2",
        expected_revision=1,
        actor={"subject": "test"},
        disposition="skipped",
    )
    assert missing.value is None
    assert missing.disposition is JournalValueDisposition.SKIPPED
    assert missing.current_revision == 2

    with pytest.raises(JournalCaptureValidationError):
        domain.put_field_value(
            value_id="value-2",
            local_date="2026-08-27",
            module_instance_id="reflection",
            module_instance_version=1,
            field_id="rating",
            field_definition_version=1,
            client_mutation_id="invalid-zero",
            expected_revision=0,
            actor={"subject": "test"},
            value=0,
        )
    with pytest.raises(JournalCaptureConflict):
        domain.put_field_value(
            value_id="value-1",
            local_date="2026-08-27",
            module_instance_id="reflection",
            module_instance_version=1,
            field_id="rating",
            field_definition_version=1,
            client_mutation_id="stale",
            expected_revision=1,
            actor={"subject": "test"},
            value=4,
        )

    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_field_value_revisions WHERE value_id='value-1'"
        ).fetchone()[0] == 2
        event = conn.execute(
            "SELECT * FROM journal_search_outbox "
            "WHERE aggregate_type='field_value' AND aggregate_id='value-1' "
            "ORDER BY aggregate_revision DESC LIMIT 1"
        ).fetchone()
        assert event is not None


def test_native_item_mutation_writes_history_and_outbox_in_same_transaction(tmp_path):
    store, domain = _domain(tmp_path)
    item = domain.create_native_item(
        local_date="2026-08-27",
        item_kind="record",
        plain_value="A record",
        source_ref="wb-source://test/item",
        interaction_behavior_id="human_value",
        interaction_behavior_version=1,
        client_mutation_id="item-create",
        actor={"subject": "test"},
    )
    assert item.current_revision == 1
    updated = domain.update_native_item(
        item_id=item.item_id,
        expected_revision=1,
        plain_value="A corrected record",
        client_mutation_id="item-update",
        actor={"subject": "test"},
    )
    assert updated.current_revision == 2
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_item_revisions WHERE item_id=?",
            (item.item_id,),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_search_outbox "
            "WHERE aggregate_type='item' AND aggregate_id=?",
            (item.item_id,),
        ).fetchone()[0] == 2


def test_generic_relations_are_revisioned_idempotent_and_search_visible(tmp_path):
    store, domain = _domain(tmp_path)
    item = domain.create_native_item(
        local_date="2026-08-27",
        item_kind="record",
        plain_value="Linked record",
        source_ref="wb-source://test/item",
        interaction_behavior_id="human_value",
        interaction_behavior_version=1,
        client_mutation_id="relation-item-create",
        actor={"subject": "test"},
    )
    first = domain.create_relation(
        source_item_id=item.item_id,
        relation_kind="supports",
        target_domain="tasks",
        target_id="task-1",
        target_revision="1",
        client_mutation_id="relation-create",
        actor={"subject": "test"},
    )
    replay = domain.create_relation(
        source_item_id=item.item_id,
        relation_kind="supports",
        target_domain="tasks",
        target_id="task-1",
        target_revision="1",
        client_mutation_id="relation-create",
        actor={"subject": "test"},
    )
    assert replay == first
    updated = domain.update_relation(
        relation_id=first.relation_id,
        expected_revision=1,
        target_revision="2",
        lifecycle="archived",
        client_mutation_id="relation-update",
        actor={"subject": "test"},
    )
    assert updated.revision == 2
    assert updated.lifecycle == "archived"
    with pytest.raises(JournalCaptureConflict):
        domain.update_relation(
            relation_id=first.relation_id,
            expected_revision=1,
            target_revision="3",
            lifecycle="current",
            client_mutation_id="relation-stale",
            actor={"subject": "test"},
        )
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_relation_revisions WHERE relation_id=?",
            (first.relation_id,),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_search_outbox "
            "WHERE aggregate_type='relation' AND aggregate_id=?",
            (first.relation_id,),
        ).fetchone()[0] == 2


def test_prompt_input_is_immutable_and_result_regeneration_is_cas_idempotent(tmp_path):
    store, domain = _domain(tmp_path)
    domain.create_prompt_definition_version(
        prompt_id="reflection",
        wording="Reflect",
    )
    domain.create_prompt_interaction(
        interaction_id="interaction-1",
        local_date="2026-08-27",
        module_instance_id="simple.notes",
        module_instance_version=1,
        prompt_id="reflection",
        prompt_version=1,
        input_text="My exact seed",
        source_ref="wb-source://test/seed",
        result_retention="all_versions",
        result_search_mode="content",
    )
    with pytest.raises(JournalCaptureConflict):
        domain.create_prompt_interaction(
            interaction_id="interaction-1",
            local_date="2026-08-27",
            module_instance_id="simple.notes",
            module_instance_version=1,
            prompt_id="reflection",
            prompt_version=1,
            input_text="Changed seed",
            source_ref="wb-source://test/seed",
            result_retention="all_versions",
            result_search_mode="content",
        )

    first = domain.record_prompt_result(
        interaction_id="interaction-1",
        expected_revision=1,
        client_mutation_id="generate-1",
        producer_id="test-provider",
        context_manifest_sha256="a" * 64,
        generation_receipt={"run": "one"},
        result_text="Generated result one",
    )
    assert domain.record_prompt_result(
        interaction_id="interaction-1",
        expected_revision=1,
        client_mutation_id="generate-1",
        producer_id="test-provider",
        context_manifest_sha256="a" * 64,
        generation_receipt={"run": "one"},
        result_text="Generated result one",
    ) == first
    second = domain.record_prompt_result(
        interaction_id="interaction-1",
        expected_revision=2,
        client_mutation_id="generate-2",
        producer_id="test-provider",
        context_manifest_sha256="b" * 64,
        generation_receipt={"run": "two"},
        result_text="Generated result two",
    )
    assert second != first
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT input_text FROM journal_prompt_interactions "
            "WHERE interaction_id='interaction-1'"
        ).fetchone()[0] == "My exact seed"
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_prompt_result_variants "
            "WHERE interaction_id='interaction-1'"
        ).fetchone()[0] == 2


def test_day_addressable_get_projects_without_writing_day_or_outbox(tmp_path, monkeypatch):
    store = JournalCaptureStore(tmp_path / "journal.db")
    sources = SourceStore.create(tmp_path / "sources")
    vault = tmp_path / "vault"
    vault.mkdir()
    service = JournalCaptureService(store, JournalContentAdapter(vault))
    monkeypatch.setattr(journal_api, "_runtime", (sources, store, service))

    with sqlite3.connect(store.path) as conn:
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "journal_days",
                "journal_day_composition_snapshots",
                "journal_search_outbox",
            )
        }

    app = Flask("journal-pure-read")
    journal_api.register_routes(app)
    response = app.test_client().get("/api/journal/view?day=2026-08-27")
    assert response.status_code == 200, response.json
    composition = response.json["view"]["effectiveComposition"]
    assert composition["persisted"] is False
    assert composition["authorityState"] == "legacy_compatibility"
    assert response.json["view"]["day"]["localDate"] == "2026-08-27"

    with sqlite3.connect(store.path) as conn:
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    assert after == before
