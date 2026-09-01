"""Durable Co-think item lifecycle behavior."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from work_buddy.cowork.verify import (
    VerifyInvariantViolation,
    cothink_items,
    current_cothink_item_status,
    record_cothink_item,
    record_cothink_item_status,
)
from work_buddy.truth.contracts import Actor
from work_buddy.truth.export import export_store, import_store
from work_buddy.truth.identity import canonical_json, sha256_bytes
from work_buddy.truth.queries import integrity_findings

from .conftest import HUMAN, NOW
from .test_verify_persistence import _capture


SYSTEM = Actor("system", "cothink-lifecycle-test")
LATER = "2026-07-17T13:00:00.000+00:00"


class _EmptyRegistry:
    def paths_for_store_id(self, _store_id: str):
        return ()


def _item(store_ctx, *, subtype: str = "question"):
    _, _, action = _capture(store_ctx)
    return record_cothink_item(
        store_ctx["store"],
        action_snapshot_id=action.id,
        subtype=subtype,
        purpose="Invite deliberate reflection",
        payload={"text": "What assumption should be tested first?"},
        rationale="Preserve useful cognitive friction.",
        provenance={"kind": "deterministic_fixture"},
        actor=SYSTEM,
        at=NOW,
    )


def test_new_item_is_open_and_same_status_is_idempotent(store_ctx):
    store = store_ctx["store"]
    item = _item(store_ctx)

    opened = current_cothink_item_status(
        store,
        cothink_item_id=item.id,
    )
    assert opened.status == "open"
    repeated = record_cothink_item_status(
        store,
        cothink_item_id=item.id,
        status="open",
        actor=HUMAN,
        reason="No transition is needed.",
        at=LATER,
    )
    assert repeated == opened
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cothink_item_status_events "
            "WHERE cothink_item_id = ?",
            (item.id,),
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE cothink_items SET purpose = 'changed' WHERE id = ?",
                (item.id,),
            )


def test_parked_item_is_kept_for_later_and_can_still_be_dismissed(store_ctx):
    store = store_ctx["store"]
    item = _item(store_ctx)

    parked = record_cothink_item_status(
        store,
        cothink_item_id=item.id,
        status="parked",
        actor=HUMAN,
        reason="Useful later, but not in the current flow.",
        at=LATER,
    )
    assert parked.status == "parked"
    assert (
        record_cothink_item_status(
            store,
            cothink_item_id=item.id,
            status="parked",
            actor=HUMAN,
        )
        == parked
    )
    dismissed = record_cothink_item_status(
        store,
        cothink_item_id=item.id,
        status="dismissed",
        actor=HUMAN,
        reason="No longer useful.",
    )
    assert dismissed.status == "dismissed"
    assert (
        record_cothink_item_status(
            store,
            cothink_item_id=item.id,
            status="dismissed",
            actor=HUMAN,
        )
        == dismissed
    )

    projection = cothink_items(store)
    assert projection[0]["lifecycle"]["status"] == "dismissed"
    assert projection[0]["lifecycle"]["event_id"] == dismissed.id
    assert "invalid_cothink_status_transition" not in {
        finding.code for finding in integrity_findings(store)
    }


def test_open_item_can_be_dismissed_and_round_trips_portably(
    store_ctx,
    tmp_path: Path,
):
    store = store_ctx["store"]
    item = _item(store_ctx)
    dismissed = record_cothink_item_status(
        store,
        cothink_item_id=item.id,
        status="dismissed",
        actor=HUMAN,
        reason="Not useful for this document.",
        at=LATER,
    )

    exported = export_store(store, tmp_path / "cothink-lifecycle.jsonl")
    target = tmp_path / "restored"
    target.mkdir()
    restored = import_store(
        exported.path,
        target,
        registry=_EmptyRegistry(),
    ).store

    current = current_cothink_item_status(
        restored,
        cothink_item_id=item.id,
    )
    assert current == dismissed
    projected = cothink_items(restored)
    assert projected[0]["id"] == item.id
    assert projected[0]["lifecycle"]["status"] == "dismissed"
    with restored.connect() as conn:
        statuses = [
            row[0]
            for row in conn.execute(
                "SELECT status FROM cothink_item_status_events "
                "WHERE cothink_item_id = ? ORDER BY rowid",
                (item.id,),
            ).fetchall()
        ]
    assert statuses == ["open", "dismissed"]


def test_v5_portable_stream_upcasts_initial_open_status(
    store_ctx,
    tmp_path: Path,
):
    item = _item(store_ctx)
    exported = export_store(
        store_ctx["store"],
        tmp_path / "current.jsonl",
    )
    objects = [
        json.loads(line)
        for line in exported.path.read_text(encoding="utf-8").splitlines()
    ]
    post_v5_records = {
        "cothink_item_status_event",
        "interaction_contract_definition",
        "document_interaction_contract_assignment",
        "document_truth_activation_transition",
        "document_truth_policy_receipt",
        "document_truth_admission_seal_event",
    }
    objects = [
        value
        for value in objects
        if value.get("record_type") not in post_v5_records
    ]
    header = objects[0]
    footer = objects[-1]
    header["format_version"] = 5
    header["store_info"]["schema_version"] = 5
    data_records = [
        value
        for value in objects[1:-1]
        if value.get("record_type") != "blob"
    ]
    footer["record_count"] = len(data_records)
    footer["last_seq"] = max(
        (int(value["seq"]) for value in data_records),
        default=0,
    )
    footer["stream_sha256"] = sha256_bytes(
        b"".join(
            (canonical_json(value) + "\n").encode("utf-8")
            for value in objects[:-1]
        )
    )
    legacy_payload = (
        "\n".join(canonical_json(value) for value in objects) + "\n"
    ).encode("utf-8")

    target = tmp_path / "legacy-restored"
    target.mkdir()
    imported = import_store(
        legacy_payload,
        target,
        registry=_EmptyRegistry(),
    )
    assert imported.source_format_version == 5
    status = current_cothink_item_status(
        imported.store,
        cothink_item_id=item.id,
    )
    assert status.status == "open"
    assert status.created_by_kind == "system"
    assert status.created_by_ref == "truth-schema-v6"
    assert (
        status.created_by_meta_json
        == '{"basis":"pre_lifecycle_item_existence"}'
    )
    with imported.store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cothink_item_status_events "
            "WHERE cothink_item_id = ?",
            (item.id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize("status", ["reopened", "archived", ""])
def test_unknown_status_is_rejected(store_ctx, status: str):
    item = _item(store_ctx)
    with pytest.raises(VerifyInvariantViolation):
        record_cothink_item_status(
            store_ctx["store"],
            cothink_item_id=item.id,
            status=status,
            actor=HUMAN,
        )
