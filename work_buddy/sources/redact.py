"""Application-level readable-content deletion with managed-copy accounting."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from work_buddy.sources.errors import InvalidSourceRequest, SourceIntegrityFailure
from work_buddy.sources.models import (
    ActorRef,
    RedactionResult,
    SourceRef,
    canonical_json,
    new_id,
    sha256_bytes,
    utc_now,
    validate_sha256,
)
from work_buddy.sources.store import SourceStore, _actor_json


_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9._-]{1,127}$")
logger = logging.getLogger(__name__)


def redact_source(
    store: SourceStore,
    *,
    source_ref: SourceRef,
    actor: ActorRef,
    authorization_fingerprint: str,
    reason_code: str,
    at: str | None = None,
) -> RedactionResult:
    """Remove readable retained content and queue every registered dependent."""

    validate_sha256(authorization_fingerprint)
    if not isinstance(reason_code, str) or not _REASON_CODE_RE.fullmatch(reason_code):
        raise InvalidSourceRequest()
    now = at or utc_now()
    blobs_to_delete: list[str] = []
    with store.write_transaction() as conn:
        item = store._get_item(conn, source_ref)
        assert item is not None
        existing_result: RedactionResult | None = None
        if item.lifecycle_state == "redacted":
            row = conn.execute(
                "SELECT * FROM source_redaction_events WHERE redaction_event_id = ?",
                (item.redaction_event_id,),
            ).fetchone()
            if row is None:
                raise SourceIntegrityFailure()
            effects = conn.execute(
                "SELECT effect_id FROM source_outbox WHERE effect_type = 'source.redaction' "
                "AND json_extract(payload_json, '$.redaction_event_id') = ? "
                "ORDER BY effect_id",
                (item.redaction_event_id,),
            ).fetchall()
            existing_result = RedactionResult(
                redaction_event_id=str(row["redaction_event_id"]),
                source_ref=source_ref,
                redaction_epoch=int(row["redaction_epoch"]),
                managed_copy_state=str(row["managed_copy_state"]),
                issued_copy_state=str(row["issued_copy_state"]),
                pending_effect_ids=tuple(str(effect["effect_id"]) for effect in effects),
                redacted_at=str(row["created_at"]),
            )

        else:
            store._find_access_binding(
                conn,
                source_ref=source_ref,
                principal=actor,
                purpose="redaction",
                access_mode="metadata",
                at=now,
            )
        # Exact run payloads may embed the source bytes inside a larger JSON
        # capability response. Their explicit derivation edges are part of the
        # erasure inventory: redact those retained copies transitively while
        # leaving semantic derivatives reviewable instead of guessing.
        cascade: list[tuple[SourceRef, Any, SourceRef | None]] = [
            (source_ref, item, None)
        ]
        seen = {source_ref.uri}
        cursor = 0
        while cursor < len(cascade):
            input_ref, _input_item, _parent = cascade[cursor]
            cursor += 1
            rows = conn.execute(
                "SELECT derived_authority_id, derived_item_id "
                "FROM source_derivations WHERE input_authority_id = ? "
                "AND input_item_id = ? AND relation = 'quoted_from' "
                "AND fidelity = 'exact_embedded_copy' ORDER BY derivation_id",
                (input_ref.authority_id, input_ref.item_id),
            ).fetchall()
            for row in rows:
                derived_ref = SourceRef(
                    str(row["derived_authority_id"]),
                    str(row["derived_item_id"]),
                )
                if derived_ref.uri in seen:
                    continue
                seen.add(derived_ref.uri)
                derived_item = store._get_item(conn, derived_ref, required=False)
                if derived_item is not None:
                    cascade.append((derived_ref, derived_item, input_ref))

        result: RedactionResult | None = existing_result
        for target_ref, target_item, upstream_ref in cascade:
            if target_item.lifecycle_state == "redacted":
                continue
            event_id = new_id()
            next_epoch = target_item.redaction_epoch + 1
            representations = conn.execute(
                "SELECT representation_id, blob_sha256 FROM source_representations "
                "WHERE authority_id = ? AND source_item_id = ?",
                (target_ref.authority_id, target_ref.item_id),
            ).fetchall()
            for representation in representations:
                blob = representation["blob_sha256"]
                conn.execute(
                    "UPDATE source_representations SET inline_content = NULL, "
                    "blob_sha256 = NULL, redacted_at = ? WHERE representation_id = ?",
                    (now, representation["representation_id"]),
                )
                if blob is not None:
                    conn.execute(
                        "UPDATE source_blobs SET ref_count = ref_count - 1 "
                        "WHERE content_sha256 = ? AND ref_count > 0",
                        (blob,),
                    )
                    count = conn.execute(
                        "SELECT ref_count FROM source_blobs WHERE content_sha256 = ?",
                        (blob,),
                    ).fetchone()
                    if count is None:
                        raise SourceIntegrityFailure()
                    if int(count["ref_count"]) == 0:
                        conn.execute(
                            "DELETE FROM source_blobs WHERE content_sha256 = ?",
                            (blob,),
                        )
                        blobs_to_delete.append(str(blob))

            usages = conn.execute(
                "SELECT * FROM source_usage_intents WHERE authority_id = ? "
                "AND source_item_id = ? AND status != 'released' ORDER BY usage_id",
                (target_ref.authority_id, target_ref.item_id),
            ).fetchall()
            pending_effect_ids: list[str] = []
            issued_copy = False
            for usage in usages:
                if usage["disclosure_kind"] in {
                    "issued_offline_copy",
                    "external_model_disclosure",
                    "external_provider_disclosure",
                }:
                    issued_copy = True
                conn.execute(
                    "UPDATE source_usage_intents SET "
                    "maintenance_state = 'pending_redaction' WHERE usage_id = ?",
                    (usage["usage_id"],),
                )
                effect_id = new_id()
                pending_effect_ids.append(effect_id)
                payload = {
                    "schema": "wb.source-redaction-effect/v1",
                    "redaction_event_id": event_id,
                    "source_ref": target_ref.to_dict(),
                    "usage_id": usage["usage_id"],
                    "consumer_domain": usage["consumer_domain"],
                    "consumer_id": usage["consumer_id"],
                    "redaction_policy": usage["redaction_policy"],
                    "redaction_epoch": next_epoch,
                }
                payload_json = canonical_json(payload)
                conn.execute(
                    "INSERT INTO source_outbox "
                    "(effect_id, target_domain, effect_type, payload_json, "
                    "payload_sha256, authorization_fingerprint, status, "
                    "created_at, updated_at) VALUES (?, ?, 'source.redaction', "
                    "?, ?, ?, 'pending', ?, ?)",
                    (
                        effect_id,
                        usage["consumer_domain"],
                        payload_json,
                        sha256_bytes(payload_json.encode("utf-8")),
                        authorization_fingerprint,
                        now,
                        now,
                    ),
                )
            managed_state = "pending" if usages else "complete"
            issued_state = (
                "uncontrolled_copies_possible" if issued_copy else "none_recorded"
            )
            conn.execute(
                "INSERT INTO source_redaction_events "
                "(redaction_event_id, authority_id, source_item_id, "
                "prior_redaction_epoch, redaction_epoch, actor_ref_json, "
                "authorization_fingerprint, reason_code, managed_copy_state, "
                "issued_copy_state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    target_ref.authority_id,
                    target_ref.item_id,
                    target_item.redaction_epoch,
                    next_epoch,
                    _actor_json(actor),
                    authorization_fingerprint,
                    reason_code,
                    managed_state,
                    issued_state,
                    now,
                ),
            )
            conn.execute(
                "UPDATE source_items SET lifecycle_state = 'redacted', "
                "redaction_epoch = ?, redaction_event_id = ? WHERE authority_id = ? "
                "AND source_item_id = ?",
                (next_epoch, event_id, target_ref.authority_id, target_ref.item_id),
            )
            conn.execute(
                "UPDATE source_access_bindings SET revoked_at = ? WHERE authority_id = ? "
                "AND source_item_id = ? AND revoked_at IS NULL",
                (now, target_ref.authority_id, target_ref.item_id),
            )
            store._add_observation(
                conn,
                target_ref,
                kind="redacted",
                resolver_id="sources-redaction",
                resolver_version="1",
                status="content_removed",
                observed_at=now,
                metadata=(
                    {}
                    if upstream_ref is None
                    else {
                        "cascade": "exact_embedded_copy",
                        "upstream_source_ref": upstream_ref.uri,
                    }
                ),
            )
            current_result = RedactionResult(
                redaction_event_id=event_id,
                source_ref=target_ref,
                redaction_epoch=next_epoch,
                managed_copy_state=managed_state,
                issued_copy_state=issued_state,
                pending_effect_ids=tuple(pending_effect_ids),
                redacted_at=now,
            )
            if target_ref == source_ref:
                result = current_result
        if result is None:
            raise SourceIntegrityFailure()
    if blobs_to_delete:
        try:
            # Serialized with every large-blob stage through the Sources
            # SQLite writer lock. If the process stops after this redaction
            # commit, SourceStore.open performs the same orphan recovery.
            store.reconcile_blobs(delete_orphans=True)
        except Exception:
            logger.warning(
                "Sources redaction blob cleanup is deferred",
                exc_info=True,
            )
    return result
