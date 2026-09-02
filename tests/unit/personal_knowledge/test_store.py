from __future__ import annotations

import sqlite3

import pytest

from work_buddy.knowledge.personal.migrations import SCHEMA_VERSION
from work_buddy.knowledge.personal.store import (
    PersonalKnowledgeIdempotencyConflict,
    PersonalKnowledgeRevisionConflict,
)
from work_buddy.knowledge.personal.service import PersonalKnowledgeService


def _create(store, path="personal/preferences/example", **overrides):
    values = {
        "logical_path": path,
        "name": "Example",
        "description": "A durable preference",
        "summary": "Use concise prose.",
        "body": "# Example\n\nUse concise prose.",
        "categories": ["preference"],
        "aliases": ["short answers"],
        "tags": ["writing", "calibration"],
        "idempotency_key": f"create:{path}",
    }
    values.update(overrides)
    return store.create_unit(**values)


def test_versioned_schema_and_document_binding_invariant(personal_store):
    assert personal_store.schema_version() == SCHEMA_VERSION == 3
    status = personal_store.authority_status()
    assert status["authority"] == "legacy_markdown"
    assert status["sealed_cohort_id"] is None

    with pytest.raises(ValueError, match="require binding"):
        personal_store.create_unit(
            logical_path="personal/reference/rich",
            name="Rich",
            body_mode="document",
            body=None,
        )
    rich = personal_store.create_unit(
        logical_path="personal/reference/rich",
        name="Rich",
        body=None,
        body_mode="document",
        document_binding_id="binding-1",
        document_store_id="store-1",
        document_id="doc-1",
        idempotency_key="rich-1",
    )
    assert personal_store.get_unit(rich["unit_id"])["body"] is None

    conn = sqlite3.connect(personal_store.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE personal_units SET body='second authority' WHERE unit_id=?",
                (rich["unit_id"],),
            )
    finally:
        conn.close()


def test_search_path_category_alias_and_privacy_filters(personal_store):
    _create(personal_store)
    _create(
        personal_store,
        "personal/work_patterns/context-switching",
        name="Context Switching",
        categories=["work_pattern", "self_regulation"],
        aliases=["tab hopping"],
        tags=["focus"],
        privacy_class="restricted",
        disclosure_class="consent_required",
        idempotency_key="create:context",
    )
    assert [row["name"] for row in personal_store.search("tab hopping")] == [
        "Context Switching"
    ]
    assert len(personal_store.search(category="self_regulation")) == 1
    assert len(personal_store.search(path_prefix="personal/work_patterns")) == 1
    assert len(personal_store.search(privacy_classes=["private"])) == 1
    assert len(
        personal_store.search(disclosure_classes=["consent_required"])
    ) == 1


def test_stable_identity_path_aliases_and_parent_child_edges(personal_store):
    parent = _create(personal_store, "personal/reference/parent")
    child = _create(
        personal_store,
        "personal/reference/child",
        parent_paths=["personal/reference/parent"],
        reference_paths=["personal/reference/parent"],
    )
    parent_row = personal_store.get_unit(parent["unit_id"])
    assert parent_row["child_paths"] == ["personal/reference/child"]
    assert personal_store.get_unit(child["unit_id"])["reference_paths"] == [
        "personal/reference/parent"
    ]

    renamed = personal_store.update_unit(
        parent["unit_id"],
        {"logical_path": "personal/reference/renamed-parent"},
        expected_revision=1,
        idempotency_key="rename-parent",
    )
    assert renamed["unit_id"] == parent["unit_id"]
    assert personal_store.get_unit("personal/reference/parent")["current_path"] == (
        "personal/reference/renamed-parent"
    )
    assert personal_store.get_unit(parent["unit_id"])["path_aliases"] == [
        "personal/reference/parent"
    ]
    assert personal_store.get_unit(child["unit_id"])["parent_paths"] == [
        "personal/reference/renamed-parent"
    ]


def test_revisions_cas_idempotency_observations_tombstones_and_outbox(personal_store):
    created = _create(personal_store)
    replay = _create(personal_store)
    assert replay == created
    with pytest.raises(PersonalKnowledgeIdempotencyConflict):
        _create(personal_store, name="Different")

    with pytest.raises(PersonalKnowledgeRevisionConflict) as exc:
        personal_store.update_unit(
            created["unit_id"], {"summary": "stale"},
            expected_revision=99, idempotency_key="stale",
        )
    assert exc.value.actual == 1

    observed = personal_store.append_observation(
        created["unit_id"], evidence="Preferred the shorter answer.",
        observed_at="2026-08-27", expected_revision=1,
        idempotency_key="observe-1",
    )
    assert observed["revision"] == 2
    row = personal_store.get_unit(created["unit_id"])
    assert row["observation_count"] == 1
    assert "Preferred the shorter answer" in row["body"]
    assert len(personal_store.observations(created["unit_id"])) == 1

    deleted = personal_store.tombstone_unit(
        created["unit_id"], expected_revision=2, idempotency_key="delete-1"
    )
    assert deleted["lifecycle"] == "tombstoned"
    assert personal_store.get_unit(created["unit_id"]) is None
    assert personal_store.get_unit(created["unit_id"], include_tombstoned=True)
    assert len(personal_store.revisions(created["unit_id"])) == 3
    outbox = personal_store.pending_outbox()
    assert [event["revision"] for event in outbox] == [1, 2, 3]
    assert outbox[-1]["event_kind"] == "delete"
    assert personal_store.mark_outbox_delivered(outbox[0]["event_id"])


def test_mint_replay_does_not_duplicate_initial_evidence(personal_store):
    service = PersonalKnowledgeService(personal_store)
    kwargs = {
        "name": "Break reminder",
        "category": "self_regulation",
        "definition": "Take a break after a long block.",
        "evidence": "A short walk helped.",
        "idempotency_key": "mint-break-reminder",
    }
    first = service.mint(**kwargs)
    second = service.mint(**kwargs)
    assert second == first
    row = personal_store.get_unit(first["unit_id"])
    assert row["observation_count"] == 1
    assert row["current_revision"] == 1
    assert len(personal_store.observations(first["unit_id"])) == 1
    with pytest.raises(PersonalKnowledgeIdempotencyConflict):
        service.mint(**{**kwargs, "definition": "Different request"})
