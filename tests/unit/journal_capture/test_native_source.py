from __future__ import annotations

import sqlite3

import pytest

from work_buddy.journal_capture.content_adapter import JournalContentAdapter
from work_buddy.journal_capture.dispatch import JournalSourceDispatcher
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.models import JournalCaptureConflict
from work_buddy.journal_capture.native_source import JournalNativeSourceService
from work_buddy.journal_capture.service import JournalCaptureService
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.sources import (
    ActorRef,
    HumanInputRequest,
    SourceStore,
    TrustedIngressContext,
    TrustedIngressService,
    redact_source,
)
from work_buddy.truth.registry import TruthStoreRegistry


def _retained_agent_output(
    tmp_path,
    *,
    exact: str = "AI generated private Journal artifact",
):
    sources = SourceStore.create(tmp_path / "sources")
    tenant = "tenant-native-source-test"
    issuer = ActorRef("test-authority", "trusted-agent-output", "service", tenant)
    human = ActorRef("test-authority", "local-profile", "human", tenant)
    service = ActorRef("test-authority", "journal-service", "service", tenant)
    context = TrustedIngressContext(
        issuer=issuer,
        issuer_version="test/v1",
        inputter=human,
        service_principal=service,
        tenant_scope_id=tenant,
        surface="journal-agent-output",
        namespace="journal-native-item",
        sensitivity_class="private",
        retention_class="durable",
        inputter_assurance="enrolled_local_session_gesture",
        authorization_fingerprint="a" * 64,
        permitted_purposes=("journal.native_item", "journal.field_value"),
    )
    committed = TrustedIngressService(sources).commit_human_input(
        context,
        HumanInputRequest(
            exact_content=exact,
            client_mutation_id="native-source-ingress-0001",
            input_mode="direct_entry",
        ),
    )
    return sources, context, committed, exact


def test_source_backed_native_item_replays_and_redaction_scrubs_all_history(tmp_path):
    journal = JournalCaptureStore(tmp_path / "journal.db")
    sources, context, committed, exact = _retained_agent_output(tmp_path)
    coordinator = JournalNativeSourceService(journal, sources)
    kwargs = {
        "source_ref": committed.source_ref,
        "representation_id": committed.representation_id,
        "service_principal": context.service_principal,
        "local_date": "2026-08-22",
        "item_kind": "generated_artifact",
        "plain_value": exact,
        "interaction_behavior_id": "provenance_only",
        "interaction_behavior_version": 1,
        "client_mutation_id": "native-source-item-create-0001",
        "actor": {"kind": "agent_run", "id": "test-agent-run"},
        "module_instance_id": "simple.notes",
        "module_instance_version": 1,
        "authorship": "ai",
        "review_state": "unreviewed",
    }

    item = coordinator.create_item(**kwargs)
    replay = coordinator.create_item(**kwargs)

    assert replay.item_id == item.item_id
    assert replay.plain_value == exact
    with journal._connect() as conn:
        dependency = conn.execute(
            "SELECT * FROM journal_native_source_dependencies"
        ).fetchone()
        assert dependency["item_id"] == item.item_id
        assert dependency["state"] == "acknowledged"
        assert conn.execute("SELECT COUNT(*) FROM journal_items").fetchone()[0] == 1
    with sources.connect() as conn:
        usage = conn.execute(
            "SELECT status,purpose,use_kind,redaction_policy "
            "FROM source_usage_intents"
        ).fetchone()
        assert tuple(usage) == (
            "acknowledged",
            "journal.native_item",
            "journal_native_item",
            "scrub",
        )

    sources.grant_access(
        source_ref=committed.source_ref,
        principal=context.inputter,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=context.authorization_fingerprint,
    )
    redaction = redact_source(
        sources,
        source_ref=committed.source_ref,
        actor=context.inputter,
        authorization_fingerprint=context.authorization_fingerprint,
        reason_code="user_requested",
    )
    dispatcher = JournalSourceDispatcher(
        sources,
        JournalCaptureService(journal, JournalContentAdapter(tmp_path / "vault")),
        service_principal=context.service_principal,
        document_registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    assert len(redaction.pending_effect_ids) == 1
    assert dispatcher.drain().delivered == 1
    assert dispatcher.drain().delivered == 0

    assert JournalDomainService(journal).list_native_items("2026-08-22") == ()
    with journal._connect() as conn:
        current = conn.execute(
            "SELECT current_plain_value,lifecycle,current_revision "
            "FROM journal_items WHERE item_id=?",
            (item.item_id,),
        ).fetchone()
        assert tuple(current) == ("[redacted]", "tombstoned", 2)
        revisions = conn.execute(
            "SELECT plain_value,lifecycle FROM journal_item_revisions "
            "WHERE item_id=? ORDER BY revision",
            (item.item_id,),
        ).fetchall()
        assert [tuple(row) for row in revisions] == [
            ("[redacted]", "tombstoned"),
            ("[redacted]", "tombstoned"),
        ]
        assert conn.execute(
            "SELECT state FROM journal_native_source_dependencies"
        ).fetchone()[0] == "released"
        receipt = conn.execute(
            "SELECT state,scrubbed_revision FROM journal_native_source_redactions"
        ).fetchone()
        assert tuple(receipt) == ("committed", 2)
        with pytest.raises(sqlite3.IntegrityError, match="is_immutable"):
            conn.execute(
                "UPDATE journal_item_revisions SET plain_value='restored' "
                "WHERE item_id=?",
                (item.item_id,),
            )
    with sources.connect() as conn:
        assert tuple(
            conn.execute(
                "SELECT status,maintenance_state FROM source_usage_intents"
            ).fetchone()
        ) == ("released", "completed")
    with pytest.raises(JournalCaptureConflict, match="Source.*removed"):
        coordinator.create_item(**kwargs)


def test_source_ack_crash_replays_bound_native_item_without_duplicate(tmp_path, monkeypatch):
    journal = JournalCaptureStore(tmp_path / "journal.db")
    sources, context, committed, exact = _retained_agent_output(tmp_path)
    coordinator = JournalNativeSourceService(journal, sources)
    acknowledge = sources.acknowledge_usage
    calls = 0

    def crash_once(usage_id: str, *, at: str | None = None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated crash before native Source acknowledgement")
        return acknowledge(usage_id, at=at)

    monkeypatch.setattr(sources, "acknowledge_usage", crash_once)
    kwargs = {
        "source_ref": committed.source_ref,
        "representation_id": committed.representation_id,
        "service_principal": context.service_principal,
        "local_date": "2026-08-22",
        "item_kind": "generated_artifact",
        "plain_value": exact,
        "interaction_behavior_id": "provenance_only",
        "interaction_behavior_version": 1,
        "client_mutation_id": "native-source-item-ack-crash-0001",
        "actor": {"kind": "agent_run", "id": "test-agent-run"},
        "authorship": "ai",
        "review_state": "unreviewed",
    }
    with pytest.raises(RuntimeError, match="simulated crash"):
        coordinator.create_item(**kwargs)
    with journal._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal_items").fetchone()[0] == 1
        assert conn.execute(
            "SELECT state FROM journal_native_source_dependencies"
        ).fetchone()[0] == "bound"

    monkeypatch.setattr(sources, "acknowledge_usage", acknowledge)
    replay = JournalNativeSourceService(journal, sources).create_item(**kwargs)
    assert replay.plain_value == exact
    with journal._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal_items").fetchone()[0] == 1
        assert conn.execute(
            "SELECT state FROM journal_native_source_dependencies"
        ).fetchone()[0] == "acknowledged"


def test_redaction_racing_before_native_item_commit_prevents_plaintext_write(
    tmp_path,
    monkeypatch,
):
    journal = JournalCaptureStore(tmp_path / "journal.db")
    sources, context, committed, exact = _retained_agent_output(tmp_path)
    sources.grant_access(
        source_ref=committed.source_ref,
        principal=context.inputter,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=context.authorization_fingerprint,
    )
    coordinator = JournalNativeSourceService(journal, sources)
    create_native_item = coordinator.domain.create_native_item
    dispatcher = JournalSourceDispatcher(
        sources,
        JournalCaptureService(journal, JournalContentAdapter(tmp_path / "vault")),
        service_principal=context.service_principal,
        document_registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    raced = False

    def redact_before_item_transaction(**kwargs):
        nonlocal raced
        if not raced:
            raced = True
            result = redact_source(
                sources,
                source_ref=committed.source_ref,
                actor=context.inputter,
                authorization_fingerprint=context.authorization_fingerprint,
                reason_code="user_requested",
            )
            assert len(result.pending_effect_ids) == 1
            assert dispatcher.drain().delivered == 1
        return create_native_item(**kwargs)

    monkeypatch.setattr(coordinator.domain, "create_native_item", redact_before_item_transaction)
    with pytest.raises(JournalCaptureConflict, match="dependency is unavailable"):
        coordinator.create_item(
            source_ref=committed.source_ref,
            representation_id=committed.representation_id,
            service_principal=context.service_principal,
            local_date="2026-08-22",
            item_kind="generated_artifact",
            plain_value=exact,
            interaction_behavior_id="provenance_only",
            interaction_behavior_version=1,
            client_mutation_id="native-source-redaction-race-0001",
            actor={"kind": "agent_run", "id": "test-agent-run"},
            authorship="ai",
            review_state="unreviewed",
        )

    with journal._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal_items").fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM journal_native_source_dependencies"
        ).fetchone()[0] == "released"
        receipt = conn.execute(
            "SELECT state,item_id FROM journal_native_source_redactions"
        ).fetchone()
        assert tuple(receipt) == ("committed", None)
    with sources.connect() as conn:
        assert tuple(
            conn.execute(
                "SELECT status,maintenance_state FROM source_usage_intents"
            ).fetchone()
        ) == ("released", "completed")


def test_source_backed_typed_field_redaction_scrubs_current_and_revision_history(
    tmp_path,
):
    journal = JournalCaptureStore(tmp_path / "journal.db")
    domain = JournalDomainService(journal)
    domain.create_field_definition_version(
        field_id="readiness",
        owner="test",
        stable_key="readiness",
        label="Readiness",
        value_kind="scale",
        constraints={"minimum": 0, "maximum": 5},
        search_mode="structured_only",
    )
    sources, context, committed, _exact = _retained_agent_output(tmp_path, exact="4")
    coordinator = JournalNativeSourceService(journal, sources)
    kwargs = {
        "source_ref": committed.source_ref,
        "representation_id": committed.representation_id,
        "service_principal": context.service_principal,
        "value_id": "readiness:2026-08-22",
        "local_date": "2026-08-22",
        "module_instance_id": "simple.notes",
        "module_instance_version": 1,
        "field_id": "readiness",
        "field_definition_version": 1,
        "client_mutation_id": "field-source-put-0001",
        "expected_revision": 0,
        "actor": {"kind": "agent_run", "id": "test-agent-run"},
        "value": 4,
        "authorship": "ai",
        "review_state": "unreviewed",
    }
    field = coordinator.put_field_value(**kwargs)
    replay = coordinator.put_field_value(**kwargs)
    assert replay == field
    assert field.value == 4.0

    sources.grant_access(
        source_ref=committed.source_ref,
        principal=context.inputter,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=context.authorization_fingerprint,
    )
    redaction = redact_source(
        sources,
        source_ref=committed.source_ref,
        actor=context.inputter,
        authorization_fingerprint=context.authorization_fingerprint,
        reason_code="user_requested",
    )
    dispatcher = JournalSourceDispatcher(
        sources,
        JournalCaptureService(journal, JournalContentAdapter(tmp_path / "vault")),
        service_principal=context.service_principal,
        document_registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    assert len(redaction.pending_effect_ids) == 1
    assert dispatcher.drain().delivered == 1

    assert domain.list_field_values("2026-08-22") == ()
    current = domain.get_field_value("readiness:2026-08-22")
    assert current.lifecycle == "tombstoned"
    assert current.value is None
    assert current.current_revision == 2
    with journal._connect() as conn:
        row = conn.execute(
            "SELECT disposition,number_value,collection_present,lifecycle "
            "FROM journal_field_values WHERE value_id='readiness:2026-08-22'"
        ).fetchone()
        assert tuple(row) == ("missing", None, 0, "tombstoned")
        revisions = conn.execute(
            "SELECT value_json,authorship,review_state "
            "FROM journal_field_value_revisions "
            "WHERE value_id='readiness:2026-08-22' ORDER BY revision"
        ).fetchall()
        assert [tuple(row) for row in revisions] == [
            ('{"redacted":true}', "ai", "unreviewed"),
            ('{"redacted":true}', "unknown", "unknown"),
        ]
        receipt = conn.execute(
            "SELECT state,value_revision,scrubbed_revision "
            "FROM journal_field_source_redactions"
        ).fetchone()
        assert tuple(receipt) == ("committed", 1, 2)
        assert conn.execute(
            "SELECT state FROM journal_field_source_dependencies"
        ).fetchone()[0] == "released"
        with pytest.raises(sqlite3.IntegrityError, match="is_immutable"):
            conn.execute(
                "UPDATE journal_field_value_revisions SET value_json='4' "
                "WHERE value_id='readiness:2026-08-22' AND revision=1"
            )
    with sources.connect() as conn:
        assert tuple(
            conn.execute(
                "SELECT status,maintenance_state FROM source_usage_intents"
            ).fetchone()
        ) == ("released", "completed")


def test_redaction_racing_before_typed_field_commit_prevents_value_write(
    tmp_path,
    monkeypatch,
):
    journal = JournalCaptureStore(tmp_path / "journal.db")
    domain = JournalDomainService(journal)
    domain.create_field_definition_version(
        field_id="readiness",
        owner="test",
        stable_key="readiness",
        label="Readiness",
        value_kind="scale",
        constraints={"minimum": 0, "maximum": 5},
    )
    sources, context, committed, _exact = _retained_agent_output(tmp_path, exact="4")
    sources.grant_access(
        source_ref=committed.source_ref,
        principal=context.inputter,
        purpose="redaction",
        access_mode="metadata",
        authorization_fingerprint=context.authorization_fingerprint,
    )
    coordinator = JournalNativeSourceService(journal, sources)
    put_field_value = coordinator.domain.put_field_value
    dispatcher = JournalSourceDispatcher(
        sources,
        JournalCaptureService(journal, JournalContentAdapter(tmp_path / "vault")),
        service_principal=context.service_principal,
        document_registry=TruthStoreRegistry(tmp_path / "truth-registry.db"),
    )
    raced = False

    def redact_before_value_transaction(**kwargs):
        nonlocal raced
        if not raced:
            raced = True
            result = redact_source(
                sources,
                source_ref=committed.source_ref,
                actor=context.inputter,
                authorization_fingerprint=context.authorization_fingerprint,
                reason_code="user_requested",
            )
            assert len(result.pending_effect_ids) == 1
            assert dispatcher.drain().delivered == 1
        return put_field_value(**kwargs)

    monkeypatch.setattr(
        coordinator.domain,
        "put_field_value",
        redact_before_value_transaction,
    )
    with pytest.raises(JournalCaptureConflict, match="dependency is unavailable"):
        coordinator.put_field_value(
            source_ref=committed.source_ref,
            representation_id=committed.representation_id,
            service_principal=context.service_principal,
            value_id="readiness:2026-08-22",
            local_date="2026-08-22",
            module_instance_id="simple.notes",
            module_instance_version=1,
            field_id="readiness",
            field_definition_version=1,
            client_mutation_id="field-source-redaction-race-0001",
            expected_revision=0,
            actor={"kind": "human", "id": "test-user"},
            value=4,
        )

    with journal._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM journal_field_values").fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM journal_field_source_dependencies"
        ).fetchone()[0] == "released"
        assert tuple(
            conn.execute(
                "SELECT state,value_revision FROM journal_field_source_redactions"
            ).fetchone()
        ) == ("committed", None)
