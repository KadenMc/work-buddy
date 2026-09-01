from __future__ import annotations

from types import SimpleNamespace

from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.partition import JournalPartition
from work_buddy.journal_capture.store import JournalCaptureStore


def test_partition_indexes_native_items_and_hydrates_current_revision(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    domain = JournalDomainService(store)
    item = domain.create_native_item(
        local_date="2026-08-27",
        item_kind="record",
        plain_value="Searchable journal phrase",
        source_ref="wb-source://test/item",
        interaction_behavior_id="human_value",
        interaction_behavior_version=1,
        client_mutation_id="create-item",
        actor={"subject": "test"},
    )
    partition = JournalPartition(store)

    refs = {ref.item_id: ref for ref in partition.discover()}
    assert f"item:{item.item_id}" in refs
    docs = partition.parse(f"item:{item.item_id}")
    assert len(docs) == 1
    assert docs[0].fields["body"] == "Searchable journal phrase"
    assert docs[0].metadata["revision"] == 1

    hydrated = partition.hydrate(
        [
            SimpleNamespace(
                doc_id=docs[0].doc_id,
                score=0.9,
                metadata={"revision": 1},
            )
        ]
    )
    assert hydrated[0]["itemId"] == item.item_id

    domain.update_native_item(
        item_id=item.item_id,
        expected_revision=1,
        plain_value="New current phrase",
        client_mutation_id="update-item",
        actor={"subject": "test"},
    )
    # A hit built for revision 1 is discarded during authority hydration.
    assert partition.hydrate(
        [
            SimpleNamespace(
                doc_id=docs[0].doc_id,
                score=0.9,
                metadata={"revision": 1},
            )
        ]
    ) == []


def test_excluded_typed_field_emits_no_search_document_or_text_outbox(tmp_path):
    store = JournalCaptureStore(tmp_path / "journal.db")
    domain = JournalDomainService(store)
    domain.create_field_definition_version(
        field_id="private-text",
        owner="test",
        stable_key="private-text",
        label="Private text",
        value_kind="short_text",
        search_mode="excluded",
    )
    secret = "must never enter the search payload"
    domain.put_field_value(
        value_id="secret-value",
        local_date="2026-08-27",
        module_instance_id="simple.notes",
        module_instance_version=1,
        field_id="private-text",
        field_definition_version=1,
        client_mutation_id="secret-create",
        expected_revision=0,
        actor={"subject": "test"},
        value=secret,
    )

    partition = JournalPartition(store)
    assert "field:secret-value" not in {ref.item_id for ref in partition.discover()}
    assert partition.parse("field:secret-value") == []
    with store._connect() as conn:
        row = conn.execute(
            "SELECT * FROM journal_search_outbox "
            "WHERE aggregate_type='field_value' AND aggregate_id='secret-value'"
        ).fetchone()
        assert row is not None
        assert secret not in " ".join(str(value) for value in row)


def test_partition_reads_legacy_bridge_from_single_entry_authority(tmp_path):
    from tests.unit.journal_capture.test_store import _capture
    from work_buddy.journal_capture.models import CaptureTarget
    import hashlib

    store = JournalCaptureStore(tmp_path / "journal.db")
    capture = _capture(store, mutation="legacy-search")
    text = "legacy bridge searchable text"
    entry = store.ensure_entry(
        capture_id=capture.capture_id,
        entry_kind=CaptureTarget.RUNNING_NOTES,
        markdown=text,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        projection_marker="legacy-search-marker",
        created_at=capture.submitted_at,
    )
    native = JournalDomainService(store).get_native_item(entry.entry_id)
    assert native.authority_kind == "legacy_entry"
    assert native.plain_value is None

    docs = JournalPartition(store).parse(f"item:{entry.entry_id}")
    assert docs[0].fields["body"] == text
