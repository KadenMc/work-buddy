"""Authorized, content-aware Sources export and staged foreign import."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.sources.errors import (
    InvalidSourceRequest,
    SourceExportDenied,
    SourceImportCollision,
    SourceImportInvalid,
)
from work_buddy.sources.models import (
    EXPORT_SCHEMA,
    ActorRef,
    SourceRef,
    canonical_json,
    canonical_sha256,
    new_id,
    sha256_bytes,
    utc_now,
    validate_sha256,
)
from work_buddy.sources.resolve import resolve_and_reserve_source
from work_buddy.sources.store import SourceStore, _actor_json


@dataclass(frozen=True, slots=True)
class ExportAuthorization:
    principal: ActorRef
    authorization_fingerprint: str
    include_content: bool = True
    purpose: str = "export"

    def __post_init__(self) -> None:
        validate_sha256(self.authorization_fingerprint)
        if self.purpose != "export":
            raise SourceExportDenied()


@dataclass(frozen=True, slots=True)
class ImportAuthorization:
    principal: ActorRef
    authorization_fingerprint: str
    allow_foreign_authorities: bool = True
    collision_policy: str = "quarantine"
    restore_operational_state: bool = False
    merge_operational_state: bool = False

    def __post_init__(self) -> None:
        validate_sha256(self.authorization_fingerprint)
        if self.collision_policy not in {"quarantine", "remap", "reject"}:
            raise InvalidSourceRequest()
        if not isinstance(self.restore_operational_state, bool) or not isinstance(
            self.merge_operational_state, bool
        ):
            raise InvalidSourceRequest()
        if self.restore_operational_state and self.merge_operational_state:
            raise InvalidSourceRequest()
        if self.merge_operational_state and self.collision_policy != "reject":
            raise InvalidSourceRequest()


@dataclass(frozen=True, slots=True)
class ExportResult:
    export_id: str
    path: Path
    sha256: str
    item_count: int
    usage_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImportResult:
    import_id: str
    item_count: int
    reused_count: int
    remapped_count: int
    quarantined_count: int
    mappings: Mapping[str, SourceRef]


def _prepared_export_usages(
    store: SourceStore, export_id: str, usage_ids: Sequence[str]
) -> dict[str, sqlite3.Row]:
    """Load the exact reservations frozen before an interrupted archive write."""

    placeholders = ",".join("?" for _ in usage_ids)
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT u.*, b.authorization_fingerprint, b.principal_ref_json "
            "FROM source_usage_intents u JOIN source_access_bindings b "
            "ON b.binding_id=u.access_binding_id WHERE u.usage_id IN ("
            f"{placeholders})",
            tuple(usage_ids),
        ).fetchall()
    finally:
        conn.close()
    if {str(row["usage_id"]) for row in rows} != set(usage_ids):
        raise SourceImportInvalid()
    by_representation: dict[str, sqlite3.Row] = {}
    for row in rows:
        representation_id = str(row["representation_id"])
        if (
            row["consumer_domain"] != "export"
            or row["consumer_id"] != export_id
            or row["use_kind"] != f"issued_export.{representation_id}"
            or row["status"] != "reserved"
            or representation_id in by_representation
        ):
            raise SourceImportInvalid()
        by_representation[representation_id] = row
    return by_representation


def _read_prepared_export_content(
    store: SourceStore,
    *,
    source_ref: SourceRef,
    representation_id: str,
    export_id: str,
    authorization: ExportAuthorization,
    recorded: Mapping[str, sqlite3.Row],
) -> tuple[str, bytes]:
    """Rebuild an absent prepared archive without minting new audit records."""

    usage = recorded.get(representation_id)
    if usage is None:
        raise SourceImportInvalid()
    expected_principal = canonical_json(authorization.principal.to_dict())
    if (
        usage["authority_id"] != source_ref.authority_id
        or usage["source_item_id"] != source_ref.item_id
        or usage["consumer_id"] != export_id
        or usage["authorization_fingerprint"]
        != authorization.authorization_fingerprint
        or usage["principal_ref_json"] != expected_principal
    ):
        raise SourceExportDenied()
    conn = store.connect()
    try:
        item = store._get_item(conn, source_ref)
        if item.lifecycle_state == "redacted":
            raise SourceImportInvalid()
        row = store._representation_row(conn, source_ref, representation_id)
        if row["redacted_at"] is not None:
            raise SourceImportInvalid()
        content = store._read_representation_row(row)
    finally:
        conn.close()
    return str(usage["usage_id"]), content


def export_sources(
    store: SourceStore,
    destination: str | Path,
    *,
    authorization: ExportAuthorization,
    source_refs: Sequence[SourceRef] | None = None,
    idempotency_key: str | None = None,
) -> ExportResult:
    """Write a restart-reconcilable snapshot after reserving issued copies."""

    refs = tuple(source_refs) if source_refs is not None else _all_refs(store)
    if len(set(refs)) != len(refs):
        raise InvalidSourceRequest()
    key = idempotency_key or new_id()
    if not isinstance(key, str) or not key or len(key) > 256:
        raise InvalidSourceRequest()
    export_id = hashlib.sha256(f"source-export:{key}".encode("utf-8")).hexdigest()[:32]
    path = Path(destination).expanduser().resolve()
    refs_json = canonical_json([ref.to_dict() for ref in refs])
    request_sha = canonical_sha256(
        {
            "destination": str(path),
            "include_content": authorization.include_content,
            "principal": authorization.principal.to_dict(),
            "source_refs": [ref.to_dict() for ref in refs],
        }
    )
    now = utc_now()
    with store.write_transaction() as conn:
        prior = conn.execute(
            "SELECT * FROM source_export_operations WHERE export_id = ?",
            (export_id,),
        ).fetchone()
        if prior is None:
            conn.execute(
                "INSERT INTO source_export_operations "
                "(export_id,idempotency_key,request_sha256,destination,include_content,"
                "source_refs_json,principal_ref_json,authorization_fingerprint,state,"
                "exported_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    export_id,
                    key,
                    request_sha,
                    str(path),
                    int(authorization.include_content),
                    refs_json,
                    _actor_json(authorization.principal),
                    authorization.authorization_fingerprint,
                    "prepared",
                    now,
                    now,
                    now,
                ),
            )
            prior = conn.execute(
                "SELECT * FROM source_export_operations WHERE export_id = ?",
                (export_id,),
            ).fetchone()
        elif prior["request_sha256"] != request_sha:
            from work_buddy.sources.errors import SourceIdempotencyConflict

            raise SourceIdempotencyConflict()
    assert prior is not None
    if prior["state"] in {"written", "completed"}:
        return _finish_written_export(store, prior)
    if prior["state"] == "prepared" and prior["payload_sha256"] is not None:
        if path.exists():
            if not path.is_file() or sha256_bytes(path.read_bytes()) != prior["payload_sha256"]:
                # The destination no longer names the exact prepared archive.
                # Never overwrite it during recovery; a human must choose a
                # new explicit operation/destination.
                raise SourceImportInvalid()
            written_at = utc_now()
            with store.write_transaction() as conn:
                conn.execute(
                    "UPDATE source_export_operations SET state='written',written_at=?,"
                    "updated_at=? WHERE export_id=? AND state='prepared'",
                    (written_at, written_at, export_id),
                )
                adopted = conn.execute(
                    "SELECT * FROM source_export_operations WHERE export_id=?",
                    (export_id,),
                ).fetchone()
            assert adopted is not None
            return _finish_written_export(store, adopted)

    selected = {(ref.authority_id, ref.item_id) for ref in refs}
    recorded_usages = (
        tuple(str(value) for value in json.loads(prior["usage_ids_json"]))
        if prior["payload_sha256"] is not None
        else ()
    )
    recorded_usage_by_representation = (
        _prepared_export_usages(store, export_id, recorded_usages)
        if recorded_usages
        else {}
    )
    resolved_content: dict[str, str | None] = {}
    usages: list[str] = []
    for ref in refs:
        item = store.get_item(ref)
        if item is None:
            from work_buddy.sources.errors import SourceNotFound

            raise SourceNotFound()
        conn = store.connect()
        try:
            representations = conn.execute(
                "SELECT representation_id, redacted_at FROM source_representations "
                "WHERE authority_id = ? AND source_item_id = ? ORDER BY representation_id",
                (ref.authority_id, ref.item_id),
            ).fetchall()
        finally:
            conn.close()
        for representation in representations:
            representation_id = str(representation["representation_id"])
            if representation["redacted_at"] is not None or not authorization.include_content:
                # Metadata-only export still requires an active export binding.
                conn = store.connect()
                try:
                    binding = store._find_access_binding(
                        conn,
                        source_ref=ref,
                        principal=authorization.principal,
                        purpose="export",
                        access_mode="metadata",
                        at=utc_now(),
                    )
                    if binding["authorization_fingerprint"] != authorization.authorization_fingerprint:
                        raise SourceExportDenied()
                finally:
                    conn.close()
                resolved_content[representation_id] = None
                continue
            if prior["payload_sha256"] is None:
                resolution = resolve_and_reserve_source(
                    store,
                    source_ref=ref,
                    representation_id=representation_id,
                    principal=authorization.principal,
                    purpose="export",
                    consumer_domain="export",
                    consumer_id=export_id,
                    use_kind=f"issued_export.{representation_id}",
                    disclosure_kind="issued_offline_copy",
                    redaction_policy="invalidate",
                )
                usage_id = resolution.reservation.usage_id
                content_bytes = resolution.resolved.content
            else:
                usage_id, content_bytes = _read_prepared_export_content(
                    store,
                    source_ref=ref,
                    representation_id=representation_id,
                    export_id=export_id,
                    authorization=authorization,
                    recorded=recorded_usage_by_representation,
                )
            # The active binding must be the exact authorization being exercised,
            # not merely another export grant held by the same principal.
            conn = store.connect()
            try:
                usage = conn.execute(
                    "SELECT b.authorization_fingerprint FROM source_usage_intents u "
                    "JOIN source_access_bindings b ON b.binding_id = u.access_binding_id "
                    "WHERE u.usage_id = ?",
                    (usage_id,),
                ).fetchone()
                if (
                    usage is None
                    or usage["authorization_fingerprint"]
                    != authorization.authorization_fingerprint
                ):
                    raise SourceExportDenied()
            finally:
                conn.close()
            usages.append(usage_id)
            resolved_content[representation_id] = base64.b64encode(content_bytes).decode(
                "ascii"
            )

    bundles = [
        _export_item(store, ref, resolved_content, selected) for ref in sorted(refs, key=lambda r: r.uri)
    ]
    header = {
        "record_type": "manifest",
        "schema": EXPORT_SCHEMA,
        "export_id": export_id,
        "exporting_authority_id": store.authority_id,
        "exported_at": str(prior["exported_at"]),
        "include_content": authorization.include_content,
        "item_count": len(bundles),
        "items_sha256": canonical_sha256(bundles),
        "authorization_fingerprint": authorization.authorization_fingerprint,
    }
    lines = [canonical_json(header)] + [
        canonical_json({"record_type": "source_item", "bundle": bundle})
        for bundle in bundles
    ]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    payload_sha = sha256_bytes(payload)
    if prior["payload_sha256"] is not None:
        if payload_sha != prior["payload_sha256"] or tuple(usages) != recorded_usages:
            raise SourceImportInvalid()
    with store.write_transaction() as conn:
        if prior["payload_sha256"] is None:
            conn.execute(
                "UPDATE source_export_operations SET payload_sha256=?,item_count=?,"
                "usage_ids_json=?,updated_at=? WHERE export_id=? AND state='prepared'",
                (
                    payload_sha,
                    len(bundles),
                    canonical_json(usages),
                    utc_now(),
                    export_id,
                ),
            )
    atomic_write_bytes(path, payload)
    written_at = utc_now()
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE source_export_operations SET state='written',written_at=?,"
            "updated_at=? WHERE export_id=? AND state='prepared'",
            (written_at, written_at, export_id),
        )
    for usage_id in usages:
        store.acknowledge_usage(usage_id)
    completed_at = utc_now()
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE source_export_operations SET state='completed',completed_at=?,"
            "updated_at=? WHERE export_id=? AND state='written'",
            (completed_at, completed_at, export_id),
        )
    return ExportResult(
        export_id=export_id,
        path=path,
        sha256=payload_sha,
        item_count=len(bundles),
        usage_ids=tuple(usages),
    )


def recover_source_export(
    store: SourceStore,
    export_id: str,
    *,
    authorization: ExportAuthorization | None = None,
) -> ExportResult:
    """Finish or safely resume one exact durable export operation."""

    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM source_export_operations WHERE export_id = ?",
            (export_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        from work_buddy.sources.errors import SourceNotFound

        raise SourceNotFound()
    if row["state"] in {"written", "completed"}:
        return _finish_written_export(store, row)
    if authorization is None:
        raise SourceExportDenied()
    refs = tuple(
        SourceRef.from_dict(value) for value in json.loads(row["source_refs_json"])
    )
    resumed_authorization = ExportAuthorization(
        principal=authorization.principal,
        authorization_fingerprint=str(row["authorization_fingerprint"]),
        include_content=bool(row["include_content"]),
    )
    return export_sources(
        store,
        row["destination"],
        authorization=resumed_authorization,
        source_refs=refs,
        idempotency_key=row["idempotency_key"],
    )


def source_export_status(store: SourceStore, export_id: str | None = None) -> tuple[dict[str, Any], ...]:
    conn = store.connect()
    try:
        if export_id is None:
            rows = conn.execute(
                "SELECT * FROM source_export_operations ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_export_operations WHERE export_id = ?",
                (export_id,),
            ).fetchall()
    finally:
        conn.close()
    return tuple(
        {
            "export_id": str(row["export_id"]),
            "destination": str(row["destination"]),
            "state": str(row["state"]),
            "payload_sha256": row["payload_sha256"],
            "item_count": row["item_count"],
            "usage_count": len(json.loads(row["usage_ids_json"])),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    )


def source_export_recovery_scope(
    store: SourceStore,
    export_id: str,
) -> dict[str, Any]:
    """Return the immutable, content-free scope a recovery approval covers."""

    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM source_export_operations WHERE export_id=?",
            (export_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        from work_buddy.sources.errors import SourceNotFound

        raise SourceNotFound()
    return {
        "export_id": str(row["export_id"]),
        "request_sha256": str(row["request_sha256"]),
        "destination": str(row["destination"]),
        "include_content": bool(row["include_content"]),
        "source_refs": [
            SourceRef.from_dict(value).uri
            for value in json.loads(row["source_refs_json"])
        ],
        "state": str(row["state"]),
        "payload_sha256": row["payload_sha256"],
        "item_count": row["item_count"],
    }


def record_source_export_operator_authorization(
    store: SourceStore,
    *,
    export_id: str,
    action: str,
    authorization_fingerprint: str,
    authorization_request_id: str,
    approved_scope: Mapping[str, Any],
) -> None:
    """Durably bind fresh high-consent approval to the frozen export scope."""

    if action not in {"recover_export", "abort_export"}:
        raise InvalidSourceRequest()
    validate_sha256(authorization_fingerprint)
    expected = source_export_recovery_scope(store, export_id)
    if dict(approved_scope) != expected:
        raise SourceExportDenied()
    with store.write_transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_export_operator_authorizations "
            "(export_id,action,authorization_fingerprint,authorization_request_id,"
            "approved_scope_sha256,created_at) VALUES(?,?,?,?,?,?)",
            (
                export_id,
                action,
                authorization_fingerprint,
                authorization_request_id,
                canonical_sha256(expected),
                utc_now(),
            ),
        )


def abort_source_export(
    store: SourceStore,
    export_id: str,
    *,
    authorization: ExportAuthorization,
    error_code: str = "operator_aborted_before_write",
) -> dict[str, Any]:
    """Fail one prepared export only when no destination copy can exist."""

    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM source_export_operations WHERE export_id=?", (export_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        from work_buddy.sources.errors import SourceNotFound

        raise SourceNotFound()
    if row["principal_ref_json"] != canonical_json(authorization.principal.to_dict()):
        raise SourceExportDenied()
    if row["state"] == "failed":
        return dict(source_export_status(store, export_id)[0])
    if row["state"] != "prepared" or Path(str(row["destination"])).exists():
        # Written/possibly-written copies must be reconciled, never declared
        # absent by an abort request.
        raise SourceImportInvalid()
    usage_ids = tuple(str(value) for value in json.loads(row["usage_ids_json"]))
    for usage_id in usage_ids:
        store.release_usage(usage_id)
    now = utc_now()
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE source_export_operations SET state='failed',error_code=?,"
            "updated_at=? WHERE export_id=? AND state='prepared'",
            (error_code, now, export_id),
        )
    return dict(source_export_status(store, export_id)[0])


def _finish_written_export(store: SourceStore, row: Any) -> ExportResult:
    path = Path(str(row["destination"]))
    expected = row["payload_sha256"]
    if not path.is_file() or not isinstance(expected, str):
        raise SourceImportInvalid()
    payload = path.read_bytes()
    if sha256_bytes(payload) != expected:
        raise SourceImportInvalid()
    usages = tuple(str(value) for value in json.loads(row["usage_ids_json"]))
    if row["state"] != "completed":
        for usage_id in usages:
            store.acknowledge_usage(usage_id)
        completed_at = utc_now()
        with store.write_transaction() as conn:
            conn.execute(
                "UPDATE source_export_operations SET state='completed',completed_at=?,"
                "updated_at=? WHERE export_id=? AND state='written'",
                (completed_at, completed_at, row["export_id"]),
            )
    return ExportResult(
        export_id=str(row["export_id"]),
        path=path,
        sha256=expected,
        item_count=int(row["item_count"]),
        usage_ids=usages,
    )


def _all_refs(store: SourceStore) -> tuple[SourceRef, ...]:
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT authority_id, source_item_id FROM source_items "
            "ORDER BY authority_id, source_item_id"
        ).fetchall()
        return tuple(SourceRef(str(row[0]), str(row[1])) for row in rows)
    finally:
        conn.close()


def _export_item(
    store: SourceStore,
    ref: SourceRef,
    content: Mapping[str, str | None],
    selected: set[tuple[str, str]],
) -> dict[str, Any]:
    conn = store.connect()
    try:
        item = conn.execute(
            "SELECT * FROM source_items WHERE authority_id = ? AND source_item_id = ?",
            (ref.authority_id, ref.item_id),
        ).fetchone()
        if item is None:
            raise SourceImportInvalid()
        representations = conn.execute(
            "SELECT * FROM source_representations WHERE authority_id = ? "
            "AND source_item_id = ? ORDER BY representation_id",
            (ref.authority_id, ref.item_id),
        ).fetchall()
        attributions = conn.execute(
            "SELECT * FROM source_attributions WHERE authority_id = ? "
            "AND source_item_id = ? ORDER BY created_at, attribution_id",
            (ref.authority_id, ref.item_id),
        ).fetchall()
        observations = conn.execute(
            "SELECT * FROM source_observations WHERE authority_id = ? "
            "AND source_item_id = ? ORDER BY observed_at, observation_id",
            (ref.authority_id, ref.item_id),
        ).fetchall()
        derivations = conn.execute(
            "SELECT * FROM source_derivations WHERE derived_authority_id = ? "
            "AND derived_item_id = ? ORDER BY derivation_id",
            (ref.authority_id, ref.item_id),
        ).fetchall()
        access = conn.execute(
            "SELECT * FROM source_access_bindings WHERE authority_id = ? "
            "AND source_item_id = ? ORDER BY binding_id",
            (ref.authority_id, ref.item_id),
        ).fetchall()
        usages = conn.execute(
            "SELECT * FROM source_usage_intents WHERE authority_id = ? "
            "AND source_item_id = ? ORDER BY usage_id",
            (ref.authority_id, ref.item_id),
        ).fetchall()
        redactions = conn.execute(
            "SELECT * FROM source_redaction_events WHERE authority_id = ? "
            "AND source_item_id = ? ORDER BY created_at, redaction_event_id",
            (ref.authority_id, ref.item_id),
        ).fetchall()
        submissions = conn.execute(
            "SELECT * FROM ingress_submissions WHERE authority_id = ? "
            "AND source_item_id = ? ORDER BY submission_id",
            (ref.authority_id, ref.item_id),
        ).fetchall()
        idempotency = []
        for row in conn.execute(
            "SELECT * FROM source_idempotency WHERE authority_id=? "
            "ORDER BY tenant_scope_id,issuer_ref_json,principal_ref_json,client_mutation_id",
            (ref.authority_id,),
        ).fetchall():
            try:
                result = json.loads(str(row["result_json"]))
                result_ref = SourceRef.from_dict(result["source_ref"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SourceImportInvalid() from exc
            if result_ref == ref:
                idempotency.append(row)
        commands: list[Any] = []
        effects: list[Any] = []
        for submission in submissions:
            command_rows = conn.execute(
                "SELECT * FROM source_commands WHERE submission_id = ? ORDER BY command_id",
                (submission["submission_id"],),
            ).fetchall()
            commands.extend(command_rows)
            for command in command_rows:
                effects.extend(
                    conn.execute(
                        "SELECT * FROM source_outbox WHERE command_id = ? ORDER BY effect_id",
                        (command["command_id"],),
                    ).fetchall()
                )
        # Redaction maintenance effects are source-owned and deliberately have
        # no ingress command.  Omitting them exported a redaction event whose
        # managed-copy state remained ``pending`` while discarding the only
        # durable recovery work that explained that state.
        usage_ids = [str(row["usage_id"]) for row in usages]
        if usage_ids:
            redaction_effects = conn.execute(
                "SELECT * FROM source_outbox WHERE effect_type='source.redaction' "
                "AND json_extract(payload_json, '$.usage_id') IN ("
                + ",".join("?" for _ in usage_ids)
                + ") ORDER BY effect_id",
                usage_ids,
            ).fetchall()
            known_effect_ids = {str(row["effect_id"]) for row in effects}
            effects.extend(
                row
                for row in redaction_effects
                if str(row["effect_id"]) not in known_effect_ids
            )
    finally:
        conn.close()

    item_dict = {key: item[key] for key in item.keys()}
    rep_dicts: list[dict[str, Any]] = []
    for row in representations:
        record = {
            key: row[key]
            for key in row.keys()
            if key not in {"inline_content", "blob_sha256"}
        }
        record["content_base64"] = content.get(str(row["representation_id"]))
        rep_dicts.append(record)
    derivation_dicts = [
        {key: row[key] for key in row.keys()}
        for row in derivations
        if (str(row["input_authority_id"]), str(row["input_item_id"])) in selected
    ]
    # Access records are portable audit only. Import always revokes them.
    access_dicts = [{key: row[key] for key in row.keys()} for row in access]
    bundle = {
        "source_ref": ref.to_dict(),
        "item": item_dict,
        "representations": rep_dicts,
        "attributions": [{key: row[key] for key in row.keys()} for row in attributions],
        "observations": [{key: row[key] for key in row.keys()} for row in observations],
        "derivations": derivation_dicts,
        "access_audit": access_dicts,
        "usages": [{key: row[key] for key in row.keys()} for row in usages],
        "redactions": [{key: row[key] for key in row.keys()} for row in redactions],
        "submissions": [{key: row[key] for key in row.keys()} for row in submissions],
        "idempotency": [
            {key: row[key] for key in row.keys()} for row in idempotency
        ],
        "commands": [{key: row[key] for key in row.keys()} for row in commands],
        "effects": [{key: row[key] for key in row.keys()} for row in effects],
    }
    bundle["record_sha256"] = canonical_sha256(bundle)
    return bundle


def import_sources(
    store: SourceStore,
    source: str | Path,
    *,
    authorization: ImportAuthorization,
) -> ImportResult:
    """Validate the complete stream, then import foreign namespaces inertly."""

    path = Path(source).expanduser().resolve()
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        records = [json.loads(line) for line in text.splitlines() if line]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceImportInvalid() from exc
    if not records or records[0].get("record_type") != "manifest":
        raise SourceImportInvalid()
    manifest = records[0]
    if manifest.get("schema") != EXPORT_SCHEMA:
        raise SourceImportInvalid()
    bundles = []
    for record in records[1:]:
        if record.get("record_type") != "source_item" or not isinstance(record.get("bundle"), dict):
            raise SourceImportInvalid()
        bundle = record["bundle"]
        expected = bundle.get("record_sha256")
        unsigned = {key: value for key, value in bundle.items() if key != "record_sha256"}
        if expected != canonical_sha256(unsigned):
            raise SourceImportInvalid()
        _validate_bundle(bundle, include_content=bool(manifest.get("include_content")))
        bundles.append(bundle)
    if len(bundles) != manifest.get("item_count") or canonical_sha256(bundles) != manifest.get(
        "items_sha256"
    ):
        raise SourceImportInvalid()
    exporting_authority = str(manifest.get("exporting_authority_id") or "")
    SourceRef(exporting_authority, new_id())
    if exporting_authority != store.authority_id and not authorization.allow_foreign_authorities:
        raise SourceExportDenied()
    if authorization.restore_operational_state:
        if exporting_authority != store.authority_id:
            raise SourceExportDenied()
        conn = store.connect()
        try:
            populated = any(
                int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("source_items", "source_imports")
            )
        finally:
            conn.close()
        if populated:
            raise SourceImportCollision()
    manifest_sha = sha256_bytes(raw)
    import_id = manifest_sha[:32]
    conn = store.connect()
    try:
        existing_import = conn.execute(
            "SELECT * FROM source_imports WHERE import_id = ?", (import_id,)
        ).fetchone()
        if existing_import is not None:
            mappings = conn.execute(
                "SELECT * FROM source_import_mappings WHERE import_id = ?",
                (import_id,),
            ).fetchall()
            return ImportResult(
                import_id=import_id,
                item_count=int(existing_import["item_count"]),
                reused_count=int(existing_import["reused_count"]),
                remapped_count=int(existing_import["remapped_count"]),
                quarantined_count=int(existing_import["quarantined_count"]),
                mappings={
                    f"{row['original_authority_id']}:{row['original_item_id']}": SourceRef(
                        str(row["local_authority_id"]), str(row["local_item_id"])
                    )
                    for row in mappings
                },
            )
    finally:
        conn.close()

    plans: list[tuple[dict[str, Any], SourceRef, str]] = []
    quarantines: list[tuple[dict[str, Any], SourceRef]] = []
    reused = 0
    remapped = 0
    for bundle in bundles:
        original = SourceRef.from_dict(bundle["source_ref"])
        existing_item = store.get_item(original)
        if existing_item is None:
            plans.append((bundle, original, "preserved"))
            continue
        if _existing_matches(store, original, bundle):
            plans.append((bundle, original, "reused"))
            reused += 1
            continue
        if authorization.collision_policy == "reject":
            raise SourceImportCollision()
        if authorization.collision_policy == "quarantine":
            quarantines.append((bundle, original))
            continue
        remapped_ref = SourceRef(store.authority_id, new_id())
        plans.append((bundle, remapped_ref, "remapped"))
        remapped += 1

    decoded_content: dict[tuple[str, str], dict[str, bytes | None]] = {}
    for bundle, target, kind in plans:
        if kind == "reused":
            continue
        by_rep: dict[str, bytes | None] = {}
        for representation in bundle["representations"]:
            encoded = representation.get("content_base64")
            if encoded is None:
                by_rep[str(representation["representation_id"])] = None
                continue
            content = base64.b64decode(encoded, validate=True)
            by_rep[str(representation["representation_id"])] = content
        decoded_content[(target.authority_id, target.item_id)] = by_rep

    mappings: dict[str, SourceRef] = {}
    now = utc_now()
    with store.write_transaction() as conn:
        staged: dict[tuple[str, str], dict[str, Any]] = {}
        for key, representations in decoded_content.items():
            staged[key] = {
                representation_id: (
                    None
                    if content is None
                    else (content, store._stage_if_needed(content, conn=conn))
                )
                for representation_id, content in representations.items()
            }
        conn.execute(
            "INSERT INTO source_imports "
            "(import_id, export_authority_id, custodian_authority_id, manifest_sha256, "
            " authorization_fingerprint, imported_at, item_count, reused_count, "
            " remapped_count, quarantined_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                import_id,
                exporting_authority,
                store.authority_id,
                manifest_sha,
                authorization.authorization_fingerprint,
                now,
                len(bundles),
                reused,
                remapped,
                len(quarantines),
            ),
        )
        for bundle, original in quarantines:
            conn.execute(
                "INSERT INTO source_import_quarantine "
                "(quarantine_id, import_id, original_authority_id, original_item_id, "
                " reason_code, record_sha256, quarantined_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    import_id,
                    original.authority_id,
                    original.item_id,
                    "identity_payload_conflict",
                    bundle["record_sha256"],
                    now,
                ),
            )
        plan_targets = {
            (
                SourceRef.from_dict(bundle["source_ref"]).authority_id,
                SourceRef.from_dict(bundle["source_ref"]).item_id,
            ): target
            for bundle, target, _mapping_kind in plans
        }
        for bundle, target, mapping_kind in plans:
            original = SourceRef.from_dict(bundle["source_ref"])
            if mapping_kind != "reused":
                _insert_bundle(
                    store,
                    conn,
                    bundle,
                    original=original,
                    target=target,
                    import_id=import_id,
                    staged=staged[(target.authority_id, target.item_id)],
                    imported_at=now,
                    restore_operational_state=(
                        authorization.restore_operational_state
                        or authorization.merge_operational_state
                    ),
                )
            elif authorization.merge_operational_state:
                _merge_operational_bundle(
                    conn,
                    bundle,
                    target=target,
                    import_id=import_id,
                    imported_at=now,
                )
            conn.execute(
                "INSERT INTO source_import_mappings "
                "(import_id, original_authority_id, original_item_id, local_authority_id, "
                " local_item_id, mapping_kind) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    import_id,
                    original.authority_id,
                    original.item_id,
                    target.authority_id,
                    target.item_id,
                    mapping_kind,
                ),
            )
            mappings[f"{original.authority_id}:{original.item_id}"] = target
        for bundle, target, mapping_kind in plans:
            if mapping_kind == "reused":
                continue
            for derivation in bundle["derivations"]:
                input_target = plan_targets.get(
                    (
                        str(derivation["input_authority_id"]),
                        str(derivation["input_item_id"]),
                    )
                )
                if input_target is None:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO source_derivations "
                    "(derivation_id, derived_authority_id, derived_item_id, "
                    " input_authority_id, input_item_id, relation, producer_ref_json, "
                    " activity_id, selector_json, method_json, fidelity, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        derivation["derivation_id"] if target == SourceRef.from_dict(bundle["source_ref"]) else new_id(),
                        target.authority_id,
                        target.item_id,
                        input_target.authority_id,
                        input_target.item_id,
                        derivation["relation"],
                        derivation["producer_ref_json"],
                        derivation["activity_id"],
                        derivation["selector_json"],
                        derivation["method_json"],
                        derivation["fidelity"],
                        derivation["created_at"],
                    ),
                )
    return ImportResult(
        import_id=import_id,
        item_count=len(bundles),
        reused_count=reused,
        remapped_count=remapped,
        quarantined_count=len(quarantines),
        mappings=mappings,
    )


def _validate_bundle(bundle: Mapping[str, Any], *, include_content: bool) -> None:
    try:
        ref = SourceRef.from_dict(bundle["source_ref"])
        item = bundle["item"]
        if item["authority_id"] != ref.authority_id or item["source_item_id"] != ref.item_id:
            raise SourceImportInvalid()
        representations = bundle["representations"]
        if not isinstance(representations, list) or not representations:
            raise SourceImportInvalid()
        primary = 0
        for representation in representations:
            if representation["authority_id"] != ref.authority_id:
                raise SourceImportInvalid()
            if representation["source_item_id"] != ref.item_id:
                raise SourceImportInvalid()
            primary += int(representation["is_primary"])
            encoded = representation.get("content_base64")
            if encoded is None:
                if include_content and representation.get("redacted_at") is None:
                    raise SourceImportInvalid()
                continue
            content = base64.b64decode(encoded, validate=True)
            if len(content) != int(representation["byte_length"]):
                raise SourceImportInvalid()
            if sha256_bytes(content) != representation["content_sha256"]:
                raise SourceImportInvalid()
        if primary != 1:
            raise SourceImportInvalid()
        idempotency = bundle.get("idempotency", [])
        if not isinstance(idempotency, list):
            raise SourceImportInvalid()
        for record in idempotency:
            if record["authority_id"] != ref.authority_id:
                raise SourceImportInvalid()
            result = json.loads(str(record["result_json"]))
            if SourceRef.from_dict(result["source_ref"]) != ref:
                raise SourceImportInvalid()
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SourceImportInvalid):
            raise
        raise SourceImportInvalid() from exc


def _existing_matches(store: SourceStore, ref: SourceRef, bundle: Mapping[str, Any]) -> bool:
    item = store.get_item(ref)
    if item is None:
        return False
    remote_item = bundle["item"]
    if (
        item.source_role != remote_item["source_role"]
        or item.fidelity != remote_item["fidelity"]
        or item.primary_representation_id != remote_item["primary_representation_id"]
    ):
        return False
    conn = store.connect()
    try:
        local = conn.execute(
            "SELECT representation_id, content_sha256, byte_length, media_type, "
            "representation_kind FROM source_representations WHERE authority_id = ? "
            "AND source_item_id = ? ORDER BY representation_id",
            (ref.authority_id, ref.item_id),
        ).fetchall()
    finally:
        conn.close()
    remote = sorted(bundle["representations"], key=lambda row: row["representation_id"])
    return [
        (
            row["representation_id"],
            row["content_sha256"],
            int(row["byte_length"]),
            row["media_type"],
            row["representation_kind"],
        )
        for row in local
    ] == [
        (
            row["representation_id"],
            row["content_sha256"],
            int(row["byte_length"]),
            row["media_type"],
            row["representation_kind"],
        )
        for row in remote
    ]


def _verify_existing_values(
    existing: sqlite3.Row,
    expected: Mapping[str, Any],
    columns: Sequence[str],
) -> None:
    if any(existing[column] != expected[column] for column in columns):
        raise SourceImportCollision()


def _merge_operational_bundle(
    conn: sqlite3.Connection,
    bundle: Mapping[str, Any],
    *,
    target: SourceRef,
    import_id: str,
    imported_at: str,
) -> None:
    """Exact-merge replay records for an already restored Source identity."""

    original = SourceRef.from_dict(bundle["source_ref"])
    if target != original:
        raise SourceImportInvalid()

    access_columns = (
        "binding_id",
        "authority_id",
        "source_item_id",
        "principal_ref_json",
        "trusted_service_id",
        "purpose",
        "access_mode",
        "scope_json",
        "external_recipient",
        "model_id",
        "egress_class",
        "content_boundary_json",
        "authorization_fingerprint",
        "gesture_receipt_id",
        "expires_at",
        "created_at",
    )
    for source in bundle["access_audit"]:
        access = dict(source)
        access["authority_id"] = target.authority_id
        access["source_item_id"] = target.item_id
        existing = conn.execute(
            "SELECT * FROM source_access_bindings WHERE binding_id=?",
            (access["binding_id"],),
        ).fetchone()
        if existing is not None:
            _verify_existing_values(existing, access, access_columns)
            continue
        conn.execute(
            "INSERT INTO source_access_bindings "
            "(binding_id,authority_id,source_item_id,principal_ref_json,trusted_service_id,"
            "purpose,access_mode,scope_json,external_recipient,model_id,egress_class,"
            "content_boundary_json,authorization_fingerprint,gesture_receipt_id,"
            "expires_at,revoked_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(access[column] for column in access_columns[:-1])
            + (imported_at, access["created_at"]),
        )

    usage_columns = (
        "usage_id",
        "authority_id",
        "source_item_id",
        "representation_id",
        "selector_json",
        "principal_ref_json",
        "purpose",
        "consumer_domain",
        "consumer_id",
        "use_kind",
        "disclosure_kind",
        "redaction_policy",
        "access_binding_id",
        "bound_redaction_epoch",
        "request_sha256",
        "status",
        "maintenance_state",
        "created_at",
        "acknowledged_at",
        "released_at",
    )
    for source in bundle["usages"]:
        usage = dict(source)
        usage["authority_id"] = target.authority_id
        usage["source_item_id"] = target.item_id
        by_id = conn.execute(
            "SELECT * FROM source_usage_intents WHERE usage_id=?",
            (usage["usage_id"],),
        ).fetchone()
        by_consumer = conn.execute(
            "SELECT * FROM source_usage_intents WHERE consumer_domain=? "
            "AND consumer_id=? AND use_kind=?",
            (usage["consumer_domain"], usage["consumer_id"], usage["use_kind"]),
        ).fetchone()
        existing = by_id or by_consumer
        if existing is not None:
            if by_id is not None and by_consumer is not None:
                if by_id["usage_id"] != by_consumer["usage_id"]:
                    raise SourceImportCollision()
            _verify_existing_values(existing, usage, usage_columns)
        else:
            conn.execute(
                "INSERT INTO source_usage_intents ("
                + ",".join(usage_columns)
                + ") VALUES ("
                + ",".join("?" for _ in usage_columns)
                + ")",
                tuple(usage[column] for column in usage_columns),
            )
        conn.execute(
            "INSERT OR IGNORE INTO source_imported_usage_audit "
            "(import_id,original_usage_id,authority_id,source_item_id,record_json,imported_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                import_id,
                usage["usage_id"],
                target.authority_id,
                target.item_id,
                canonical_json(usage),
                imported_at,
            ),
        )

    submission_columns = (
        "submission_id",
        "authority_id",
        "source_item_id",
        "representation_id",
        "issuer_ref_json",
        "inputter_ref_json",
        "input_mode",
        "gesture_receipt_id",
        "authorization_fingerprint",
        "occurred_at",
        "received_at",
        "committed_at",
    )
    for source in bundle["submissions"]:
        submission = dict(source)
        submission["authority_id"] = target.authority_id
        submission["source_item_id"] = target.item_id
        existing = conn.execute(
            "SELECT * FROM ingress_submissions WHERE submission_id=?",
            (submission["submission_id"],),
        ).fetchone()
        if existing is not None:
            _verify_existing_values(existing, submission, submission_columns)
        else:
            conn.execute(
                "INSERT INTO ingress_submissions ("
                + ",".join(submission_columns)
                + ") VALUES ("
                + ",".join("?" for _ in submission_columns)
                + ")",
                tuple(submission[column] for column in submission_columns),
            )

    idempotency_columns = (
        "authority_id",
        "tenant_scope_id",
        "issuer_ref_json",
        "principal_ref_json",
        "client_mutation_id",
        "request_sha256",
        "result_json",
        "created_at",
    )
    for source in bundle.get("idempotency", []):
        record = dict(source)
        record["authority_id"] = target.authority_id
        result = json.loads(str(record["result_json"]))
        if SourceRef.from_dict(result["source_ref"]) != target:
            raise SourceImportInvalid()
        existing = conn.execute(
            "SELECT * FROM source_idempotency WHERE authority_id=? "
            "AND tenant_scope_id=? AND issuer_ref_json=? AND principal_ref_json=? "
            "AND client_mutation_id=?",
            tuple(record[column] for column in idempotency_columns[:5]),
        ).fetchone()
        if existing is not None:
            _verify_existing_values(existing, record, idempotency_columns)
        else:
            conn.execute(
                "INSERT INTO source_idempotency ("
                + ",".join(idempotency_columns)
                + ") VALUES ("
                + ",".join("?" for _ in idempotency_columns)
                + ")",
                tuple(record[column] for column in idempotency_columns),
            )


def _insert_bundle(
    store: SourceStore,
    conn: Any,
    bundle: Mapping[str, Any],
    *,
    original: SourceRef,
    target: SourceRef,
    import_id: str,
    staged: Mapping[str, Any],
    imported_at: str,
    restore_operational_state: bool = False,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO source_authorities "
        "(authority_id, custody_kind, imported_at, import_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            target.authority_id,
            "local" if target.authority_id == store.authority_id else "foreign",
            imported_at,
            import_id,
            imported_at,
        ),
    )
    item = dict(bundle["item"])
    redaction_id_map = {
        str(redaction["redaction_event_id"]): (
            str(redaction["redaction_event_id"]) if target == original else new_id()
        )
        for redaction in bundle["redactions"]
    }
    representation_id_map: dict[str, str] = {}
    for representation in bundle["representations"]:
        old = str(representation["representation_id"])
        representation_id_map[old] = old if target == original else new_id()
    primary = representation_id_map[str(item["primary_representation_id"])]
    primary_content_missing = staged[str(item["primary_representation_id"])] is None
    lifecycle_state = str(item["lifecycle_state"])
    if primary_content_missing and lifecycle_state == "active":
        lifecycle_state = "tombstoned"
    conn.execute(
        "INSERT INTO source_items "
        "(authority_id, source_item_id, custodian_authority_id, ref_schema, "
        " primary_representation_id, origin_ref_json, native_revision, source_role, fidelity, "
        " tenant_scope_id, originating_surface, namespace, sensitivity_class, "
        " retention_class, occurred_at, provider_observed_at, received_at, committed_at, "
        " lifecycle_state, redaction_epoch, redaction_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target.authority_id,
            target.item_id,
            store.authority_id,
            item["ref_schema"],
            primary,
            item["origin_ref_json"],
            item["native_revision"],
            item["source_role"],
            item["fidelity"],
            item["tenant_scope_id"],
            item["originating_surface"],
            item["namespace"],
            item["sensitivity_class"],
            item["retention_class"],
            item["occurred_at"],
            item["provider_observed_at"],
            item["received_at"],
            item["committed_at"],
            lifecycle_state,
            item["redaction_epoch"],
            redaction_id_map.get(str(item["redaction_event_id"]))
            if item["redaction_event_id"]
            else None,
        ),
    )
    for representation in bundle["representations"]:
        old_id = str(representation["representation_id"])
        target_id = representation_id_map[old_id]
        staged_value = staged[old_id]
        inline_content = None
        blob_sha = None
        if staged_value is not None:
            content, blob = staged_value
            if blob is None:
                inline_content = content
            else:
                blob_sha = blob.sha256
                conn.execute(
                    "INSERT INTO source_blobs "
                    "(content_sha256, relative_path, byte_length, ref_count, created_at) "
                    "VALUES (?, ?, ?, 1, ?) ON CONFLICT(content_sha256) "
                    "DO UPDATE SET ref_count = ref_count + 1",
                    (blob.sha256, blob.relative_path, blob.byte_length, imported_at),
                )
        if representation.get("redacted_at") is None and staged_value is None:
            # A metadata-only export cannot become a readable local record.  Keep
            # the identity as a content-unavailable tombstone.
            redacted_at = imported_at
        else:
            redacted_at = representation.get("redacted_at")
        conn.execute(
            "INSERT INTO source_representations "
            "(representation_id, authority_id, source_item_id, representation_kind, "
            " media_type, schema_type, character_encoding, content_sha256, byte_length, "
            " character_length, inline_content, blob_sha256, is_primary, "
            " derived_from_representation_id, derivation_relation, producer_ref_json, "
            " created_at, redacted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                target_id,
                target.authority_id,
                target.item_id,
                representation["representation_kind"],
                representation["media_type"],
                representation["schema_type"],
                representation["character_encoding"],
                representation["content_sha256"],
                representation["byte_length"],
                representation["character_length"],
                inline_content,
                blob_sha,
                representation["is_primary"],
                (
                    representation_id_map.get(representation["derived_from_representation_id"])
                    if representation["derived_from_representation_id"]
                    else None
                ),
                representation["derivation_relation"],
                representation["producer_ref_json"],
                representation["created_at"],
                redacted_at,
            ),
        )
    attribution_id_map = {
        str(attribution["attribution_id"]): (
            str(attribution["attribution_id"]) if target == original else new_id()
        )
        for attribution in bundle["attributions"]
    }
    for attribution in bundle["attributions"]:
        conn.execute(
            "INSERT OR IGNORE INTO source_attributions "
            "(attribution_id, authority_id, source_item_id, representation_id, role, "
            " actor_ref_json, attribution_state, basis, assurance, selector_json, "
            " asserted_by_json, observed_at, supersedes_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attribution_id_map[str(attribution["attribution_id"])],
                target.authority_id,
                target.item_id,
                representation_id_map.get(attribution["representation_id"]),
                attribution["role"],
                attribution["actor_ref_json"],
                attribution["attribution_state"],
                attribution["basis"],
                attribution["assurance"],
                attribution["selector_json"],
                attribution["asserted_by_json"],
                attribution["observed_at"],
                attribution_id_map.get(str(attribution["supersedes_id"]))
                if attribution["supersedes_id"]
                else None,
                attribution["created_at"],
            ),
        )
    for redaction in bundle["redactions"]:
        conn.execute(
            "INSERT OR IGNORE INTO source_redaction_events "
            "(redaction_event_id, authority_id, source_item_id, prior_redaction_epoch, "
            " redaction_epoch, actor_ref_json, authorization_fingerprint, reason_code, "
            " managed_copy_state, issued_copy_state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                redaction_id_map[str(redaction["redaction_event_id"])],
                target.authority_id,
                target.item_id,
                redaction["prior_redaction_epoch"],
                redaction["redaction_epoch"],
                redaction["actor_ref_json"],
                redaction["authorization_fingerprint"],
                redaction["reason_code"],
                redaction["managed_copy_state"],
                redaction["issued_copy_state"],
                redaction["created_at"],
            ),
        )
    for usage in bundle["usages"]:
        usage_record = dict(usage)
        usage_record["authority_id"] = target.authority_id
        usage_record["source_item_id"] = target.item_id
        conn.execute(
            "INSERT INTO source_imported_usage_audit "
            "(import_id, original_usage_id, authority_id, source_item_id, "
            " record_json, imported_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                import_id,
                usage["usage_id"],
                target.authority_id,
                target.item_id,
                canonical_json(usage_record),
                imported_at,
            ),
        )
    for observation in bundle["observations"]:
        conn.execute(
            "INSERT OR IGNORE INTO source_observations "
            "(observation_id, authority_id, source_item_id, observation_kind, resolver_id, "
            " resolver_version, observed_at, native_revision, native_content_sha256, "
            " retained_sha256, status, error_code, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                observation["observation_id"] if target == original else new_id(),
                target.authority_id,
                target.item_id,
                observation["observation_kind"],
                observation["resolver_id"],
                observation["resolver_version"],
                observation["observed_at"],
                observation["native_revision"],
                observation["native_content_sha256"],
                observation["retained_sha256"],
                observation["status"],
                observation["error_code"],
                observation["metadata_json"],
            ),
        )
    # Imported access grants remain explicit audit but are always revoked.
    for access in bundle["access_audit"]:
        conn.execute(
            "INSERT OR IGNORE INTO source_access_bindings "
            "(binding_id, authority_id, source_item_id, principal_ref_json, trusted_service_id, "
            " purpose, access_mode, scope_json, external_recipient, model_id, egress_class, "
            " content_boundary_json, authorization_fingerprint, gesture_receipt_id, "
            " expires_at, revoked_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                access["binding_id"] if target == original else new_id(),
                target.authority_id,
                target.item_id,
                access["principal_ref_json"],
                access["trusted_service_id"],
                access["purpose"],
                access["access_mode"],
                access["scope_json"],
                access["external_recipient"],
                access["model_id"],
                access["egress_class"],
                access["content_boundary_json"],
                access["authorization_fingerprint"],
                access["gesture_receipt_id"],
                access["expires_at"],
                imported_at,
                access["created_at"],
            ),
        )
    if restore_operational_state:
        if target != original:
            raise SourceImportInvalid()
        for usage in bundle["usages"]:
            conn.execute(
                "INSERT INTO source_usage_intents "
                "(usage_id,authority_id,source_item_id,representation_id,selector_json,"
                "principal_ref_json,purpose,consumer_domain,consumer_id,use_kind,"
                "disclosure_kind,redaction_policy,access_binding_id,bound_redaction_epoch,"
                "request_sha256,status,maintenance_state,created_at,acknowledged_at,released_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    usage["usage_id"],
                    usage["authority_id"],
                    usage["source_item_id"],
                    usage["representation_id"],
                    usage["selector_json"],
                    usage["principal_ref_json"],
                    usage["purpose"],
                    usage["consumer_domain"],
                    usage["consumer_id"],
                    usage["use_kind"],
                    usage["disclosure_kind"],
                    usage["redaction_policy"],
                    usage["access_binding_id"],
                    usage["bound_redaction_epoch"],
                    usage["request_sha256"],
                    usage["status"],
                    usage["maintenance_state"],
                    usage["created_at"],
                    usage["acknowledged_at"],
                    usage["released_at"],
                ),
            )
        for idempotency in bundle.get("idempotency", []):
            result = json.loads(str(idempotency["result_json"]))
            if SourceRef.from_dict(result["source_ref"]) != target:
                raise SourceImportInvalid()
            conn.execute(
                "INSERT INTO source_idempotency "
                "(authority_id,tenant_scope_id,issuer_ref_json,principal_ref_json,"
                "client_mutation_id,request_sha256,result_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    target.authority_id,
                    idempotency["tenant_scope_id"],
                    idempotency["issuer_ref_json"],
                    idempotency["principal_ref_json"],
                    idempotency["client_mutation_id"],
                    idempotency["request_sha256"],
                    idempotency["result_json"],
                    idempotency["created_at"],
                ),
            )
    # Operational work never activates on import. Commands and effects retain
    # their reference-only payload as paused audit records.
    submission_map: dict[str, str] = {}
    for submission in bundle["submissions"]:
        submission_id = submission["submission_id"] if target == original else new_id()
        submission_map[submission["submission_id"]] = submission_id
        conn.execute(
            "INSERT OR IGNORE INTO ingress_submissions "
            "(submission_id, authority_id, source_item_id, representation_id, issuer_ref_json, "
            " inputter_ref_json, input_mode, gesture_receipt_id, authorization_fingerprint, "
            " occurred_at, received_at, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                submission_id,
                target.authority_id,
                target.item_id,
                representation_id_map[submission["representation_id"]],
                submission["issuer_ref_json"],
                submission["inputter_ref_json"],
                submission["input_mode"],
                submission["gesture_receipt_id"],
                submission["authorization_fingerprint"],
                submission["occurred_at"],
                submission["received_at"],
                submission["committed_at"],
            ),
        )
    command_map: dict[str, str] = {}
    for command in bundle["commands"]:
        command_id = command["command_id"] if target == original else new_id()
        command_map[command["command_id"]] = command_id
        conn.execute(
            "INSERT OR IGNORE INTO source_commands "
            "(command_id, submission_id, command_schema, target_domain, command_type, "
            " parameters_json, parameters_sha256, authorization_fingerprint, "
            " authorization_expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                command_id,
                submission_map[command["submission_id"]],
                command["command_schema"],
                command["target_domain"],
                command["command_type"],
                command["parameters_json"],
                command["parameters_sha256"],
                command["authorization_fingerprint"],
                command["authorization_expires_at"],
                command["created_at"],
            ),
        )
    for effect in bundle["effects"]:
        original_command_id = effect["command_id"]
        terminal = effect["status"] in {"succeeded", "failed_terminal"}
        restored_status = effect["status"] if terminal else "paused"
        restored_error = effect["error_code"] if terminal else "imported_inert"
        conn.execute(
            "INSERT OR IGNORE INTO source_outbox "
            "(effect_id, command_id, target_domain, effect_type, payload_json, payload_sha256, "
            " authorization_fingerprint, authorization_expires_at, status, attempts, "
            " result_ref, result_sha256, error_code, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                effect["effect_id"] if target == original else new_id(),
                (
                    None
                    if original_command_id is None
                    else command_map[original_command_id]
                ),
                effect["target_domain"],
                effect["effect_type"],
                effect["payload_json"],
                effect["payload_sha256"],
                effect["authorization_fingerprint"],
                effect["authorization_expires_at"],
                restored_status,
                effect["attempts"],
                effect["result_ref"],
                effect["result_sha256"],
                restored_error,
                effect["created_at"],
                imported_at,
            ),
        )
