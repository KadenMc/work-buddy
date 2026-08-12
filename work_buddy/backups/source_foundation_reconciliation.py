"""Conservative cohort checks and explicit recovery-fence reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from work_buddy.backups.source_foundation_restore import (
    RESTORE_FENCE_FILENAME,
    read_restore_fence,
    restore_fence_lock,
    write_restore_fence,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


@dataclass(frozen=True, slots=True)
class SourceFoundationPaths:
    agent_execution_db: Path
    cowork_conversation_source_dependencies_db: Path
    conversations_db: Path
    sources_root: Path
    local_identity_db: Path
    journal_capture_db: Path
    task_note_migration_db: Path
    truth_registry_db: Path

    @classmethod
    def current(cls) -> "SourceFoundationPaths":
        from work_buddy.paths import data_dir, resolve

        return cls(
            agent_execution_db=resolve("db/agent-execution"),
            cowork_conversation_source_dependencies_db=resolve(
                "db/cowork-conversation-source-dependencies"
            ),
            conversations_db=data_dir("agents") / "conversations.db",
            sources_root=resolve("stores/sources"),
            local_identity_db=resolve("db/local-identity"),
            journal_capture_db=resolve("db/journal-capture"),
            task_note_migration_db=resolve("db/task-note-migration"),
            truth_registry_db=resolve("db/truth-registry"),
        )


def _finding(code: str, detail: str, **context: Any) -> dict[str, Any]:
    return {"code": code, "detail": detail, "context": context}


def _sqlite_integrity(path: Path, *, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return [_finding(f"{label}_missing", f"{label} state is missing", path=str(path))]
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [
            _finding(
                f"{label}_unreadable",
                f"{label} state cannot be inspected",
                error=type(exc).__name__,
            )
        ]
    findings: list[dict[str, Any]] = []
    if integrity != [("ok",)]:
        findings.append(
            _finding(
                f"{label}_integrity_failed",
                f"{label} failed SQLite integrity validation",
            )
        )
    if foreign_keys:
        findings.append(
            _finding(
                f"{label}_foreign_key_failed",
                f"{label} has unresolved foreign-key violations",
                count=len(foreign_keys),
            )
        )
    return findings


def _identity_values(path: Path) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT key,value FROM local_identity_meta WHERE key IN "
            "('schema_version','issuer_authority_id','tenant_scope_id','local_actor_id')"
        ).fetchall()
    finally:
        conn.close()
    values = {str(key): str(value) for key, value in rows}
    if set(values) != {
        "schema_version",
        "issuer_authority_id",
        "tenant_scope_id",
        "local_actor_id",
    }:
        raise ValueError("local_identity_enrollment_incomplete")
    return values


def _read_sanitized_identity_enrollment(
    enrollment_path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[Path, dict[str, Any], str]:
    """Parse the deliberately narrow, session-free enrollment envelope."""

    path = Path(enrollment_path).expanduser().resolve()
    payload_bytes = path.read_bytes()
    digest = _sha256_bytes(payload_bytes)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("local_identity_enrollment_digest_mismatch")
    value = json.loads(payload_bytes.decode("utf-8"))
    expected_keys = {
        "schema",
        "schema_version",
        "issuer_authority_id",
        "tenant_scope_id",
        "local_actor_id",
        "restores_live_sessions",
        "trust_required_before_identity_reuse",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("schema") != "wb.local-identity-enrollment-export/v1"
        or value.get("restores_live_sessions") is not False
        or value.get("trust_required_before_identity_reuse") is not True
    ):
        raise ValueError("local_identity_enrollment_invalid")
    for key in (
        "schema_version",
        "issuer_authority_id",
        "tenant_scope_id",
        "local_actor_id",
    ):
        candidate = value.get(key)
        if (
            not isinstance(candidate, str)
            or not candidate
            or len(candidate) > 256
            or any(ord(character) < 0x20 for character in candidate)
        ):
            raise ValueError("local_identity_enrollment_invalid")
    return path, value, digest


def validate_sanitized_identity_enrollment(
    enrollment_path: str | Path,
    *,
    local_identity_db: Path,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate stable enrollment IDs without importing authority rows."""

    _path, value, digest = _read_sanitized_identity_enrollment(
        enrollment_path,
        expected_sha256=expected_sha256,
    )
    current = _identity_values(local_identity_db)
    for key in (
        "schema_version",
        "issuer_authority_id",
        "tenant_scope_id",
        "local_actor_id",
    ):
        if str(value.get(key)) != current[key]:
            raise ValueError("local_identity_enrollment_mismatch")
    stable_identity = {
        key: current[key]
        for key in (
            "schema_version",
            "issuer_authority_id",
            "tenant_scope_id",
            "local_actor_id",
        )
    }
    return {
        "schema": "wb.source-foundation-identity-trust/v1",
        "enrollment_sha256": digest,
        "stable_identity_sha256": _sha256_json(stable_identity),
        "trusted_at": _now(),
        "restored_sessions": False,
        "restored_gestures": False,
    }


def reconstitute_sanitized_identity(
    enrollment_path: str | Path,
    *,
    local_identity_db: Path,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Publish stable enrollment IDs into a new authority with no credentials.

    This is deliberately *not* an identity-database import.  The current schema
    is built from trusted code, only the four stable enrollment values are
    copied from the digest-bound envelope, and every session/CSRF/bootstrap/
    gesture table is proven empty before the database is atomically published.
    """

    _path, value, digest = _read_sanitized_identity_enrollment(
        enrollment_path,
        expected_sha256=expected_sha256,
    )
    target = local_identity_db.expanduser().resolve()
    if target.exists():
        raise ValueError("local_identity_reconstitution_requires_missing_target")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.reconstituting-",
        suffix=".db",
        dir=target.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        from work_buddy.security.local_identity import LocalIdentityAuthority

        authority = LocalIdentityAuthority(temporary)
        conn = authority._connect()  # trusted recovery schema boundary
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for key in (
                    "schema_version",
                    "issuer_authority_id",
                    "tenant_scope_id",
                    "local_actor_id",
                ):
                    conn.execute(
                        "UPDATE local_identity_meta SET value=? WHERE key=?",
                        (str(value[key]), key),
                    )
                credential_tables = (
                    "local_bootstrap_tokens",
                    "local_browser_sessions",
                    "local_session_csrf_tokens",
                    "local_gesture_challenges",
                )
                counts = {
                    table: int(
                        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
                    for table in credential_tables
                }
                if any(counts.values()):
                    raise ValueError("local_identity_reconstitution_minted_authority")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        if target.exists():
            raise ValueError("local_identity_reconstitution_target_changed")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    stable_identity = {
        key: str(value[key])
        for key in (
            "schema_version",
            "issuer_authority_id",
            "tenant_scope_id",
            "local_actor_id",
        )
    }
    return {
        "schema": "wb.source-foundation-identity-reconstitution/v1",
        "enrollment_sha256": digest,
        "stable_identity_sha256": _sha256_json(stable_identity),
        "reconstituted_at": _now(),
        "restored_sessions": False,
        "restored_gestures": False,
    }


def record_identity_trust(
    enrollment_path: str | Path,
    *,
    marker_path: str | Path | None = None,
    paths: SourceFoundationPaths | None = None,
) -> dict[str, Any]:
    """Persist an explicit trust decision without restoring session authority."""

    with restore_fence_lock(marker_path):
        fence = read_restore_fence(marker_path)
        if not fence.active or not fence.valid or fence.payload is None:
            raise ValueError(fence.error or "source_foundation_restore_fence_unavailable")
        current_paths = paths or SourceFoundationPaths.current()
        expected = fence.payload.get("identity_enrollment")
        expected_digest = (
            expected.get("sha256") if isinstance(expected, Mapping) else None
        )
        evidence = validate_sanitized_identity_enrollment(
            enrollment_path,
            local_identity_db=current_paths.local_identity_db,
            expected_sha256=str(expected_digest) if expected_digest else None,
        )
        updated = dict(fence.payload)
        reconciliation = dict(updated.get("reconciliation") or {})
        reconciliation.update({"state": "pending", "identity_trust": evidence})
        updated["reconciliation"] = reconciliation
        write_restore_fence(updated, path=fence.path)
    return evidence


def _inspect_identity(
    payload: Mapping[str, Any],
    paths: SourceFoundationPaths,
) -> list[dict[str, Any]]:
    findings = _sqlite_integrity(paths.local_identity_db, label="local_identity")
    if findings:
        return findings
    reconciliation = payload.get("reconciliation")
    trust = (
        reconciliation.get("identity_trust")
        if isinstance(reconciliation, Mapping)
        else None
    )
    if not isinstance(trust, Mapping):
        return [
            _finding(
                "local_identity_trust_required",
                "The sanitized enrollment has not been explicitly trusted",
            )
        ]
    try:
        current = _identity_values(paths.local_identity_db)
    except (sqlite3.Error, ValueError) as exc:
        return [
            _finding(
                "local_identity_enrollment_invalid",
                "The live stable enrollment cannot be validated",
                error=type(exc).__name__,
            )
        ]
    if trust.get("stable_identity_sha256") != _sha256_json(current):
        return [
            _finding(
                "local_identity_trust_stale",
                "The trusted enrollment does not match the live stable identity",
            )
        ]
    expected = payload.get("identity_enrollment")
    if (
        isinstance(expected, Mapping)
        and expected.get("sha256")
        and trust.get("enrollment_sha256") != expected.get("sha256")
    ):
        return [
            _finding(
                "local_identity_snapshot_mismatch",
                "The trusted enrollment did not come from this restore snapshot",
            )
        ]
    if trust.get("restored_sessions") is not False or trust.get("restored_gestures") is not False:
        return [
            _finding(
                "local_identity_live_authority_import_forbidden",
                "A trust receipt may not claim restored sessions or gestures",
            )
        ]
    return []


def _inspect_agent_sources(
    paths: SourceFoundationPaths,
    restore_payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    findings = _sqlite_integrity(paths.agent_execution_db, label="agent_execution")
    sources_db = paths.sources_root / "store.db"
    findings.extend(_sqlite_integrity(sources_db, label="sources"))
    if findings:
        return findings
    agent = sqlite3.connect(
        f"file:{paths.agent_execution_db.resolve()}?mode=ro", uri=True
    )
    sources = sqlite3.connect(f"file:{sources_db.resolve()}?mode=ro", uri=True)
    agent.row_factory = sqlite3.Row
    sources.row_factory = sqlite3.Row
    try:
        store_info = sources.execute(
            "SELECT authority_id FROM source_store_info WHERE singleton=1"
        ).fetchall()
        if len(store_info) != 1 or sources.execute(
            "SELECT 1 FROM source_authorities WHERE authority_id=?",
            (str(store_info[0]["authority_id"]) if store_info else "",),
        ).fetchone() is None:
            findings.append(
                _finding(
                    "sources_authority_invalid",
                    "Sources has no single registered minting authority",
                )
            )

        representations = sources.execute(
            "SELECT representation_id,content_sha256,byte_length,inline_content,"
            "blob_sha256,redacted_at FROM source_representations "
            "ORDER BY representation_id"
        ).fetchall()
        blob_rows = sources.execute(
            "SELECT content_sha256,relative_path,byte_length,ref_count "
            "FROM source_blobs ORDER BY content_sha256"
        ).fetchall()
        referenced_counts: dict[str, int] = {}
        for representation in representations:
            representation_id = str(representation["representation_id"])
            inline = representation["inline_content"]
            blob_digest = representation["blob_sha256"]
            if representation["redacted_at"] is not None:
                if inline is not None or blob_digest is not None:
                    findings.append(
                        _finding(
                            "sources_redacted_representation_retained",
                            "A redacted Sources representation still retains bytes",
                            representation_id=representation_id,
                        )
                    )
                continue
            if (inline is None) == (blob_digest is None):
                findings.append(
                    _finding(
                        "sources_representation_storage_invalid",
                        "A live Sources representation has no single retained payload",
                        representation_id=representation_id,
                    )
                )
                continue
            expected_digest = str(representation["content_sha256"])
            expected_length = int(representation["byte_length"])
            if inline is not None:
                content = bytes(inline)
                if len(content) != expected_length or _sha256_bytes(content) != expected_digest:
                    findings.append(
                        _finding(
                            "sources_inline_representation_mismatch",
                            "A retained inline representation fails its digest boundary",
                            representation_id=representation_id,
                        )
                    )
            else:
                digest = str(blob_digest)
                referenced_counts[digest] = referenced_counts.get(digest, 0) + 1

        blob_root = (paths.sources_root / "blobs").resolve()
        registered_digests: set[str] = set()
        for blob in blob_rows:
            digest = str(blob["content_sha256"])
            registered_digests.add(digest)
            expected_relative = f"{digest[:2]}/{digest}"
            relative = str(blob["relative_path"]).replace("\\", "/")
            candidate = (blob_root / relative).resolve()
            valid_path = candidate.parent == (blob_root / digest[:2]).resolve()
            try:
                content = candidate.read_bytes() if valid_path else b""
            except OSError:
                content = b""
            if (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or relative != expected_relative
                or not valid_path
                or len(content) != int(blob["byte_length"])
                or _sha256_bytes(content) != digest
                or int(blob["ref_count"]) != referenced_counts.get(digest, 0)
            ):
                findings.append(
                    _finding(
                        "sources_blob_cohort_mismatch",
                        "A registered Sources blob fails path, digest, length, or reference validation",
                        content_sha256=digest,
                    )
                )
        missing_registrations = set(referenced_counts) - registered_digests
        if missing_registrations:
            findings.append(
                _finding(
                    "sources_blob_registration_missing",
                    "A Sources representation references an unregistered blob",
                    content_sha256=sorted(missing_registrations),
                )
            )

        entries = agent.execute(
            "SELECT * FROM agent_execution_disclosure_entries ORDER BY created_at,id"
        ).fetchall()
        for entry in entries:
            if entry["state"] == "possibly_sent":
                findings.append(
                    _finding(
                        "agent_disclosure_possibly_sent",
                        "An ambiguous transport outcome must be resolved without replay",
                        entry_id=str(entry["id"]),
                        run_id=str(entry["run_id"]),
                    )
                )
            elif int(entry["send_attempted"]) and entry["source_acknowledgement"] != "acknowledged":
                findings.append(
                    _finding(
                        "agent_disclosure_ack_pending",
                        "A known transport outcome still needs Sources acknowledgement",
                        entry_id=str(entry["id"]),
                        state=str(entry["state"]),
                    )
                )
            try:
                from work_buddy.sources.models import SourceRef

                ref = SourceRef.parse(str(entry["source_ref"]))
            except Exception:
                findings.append(
                    _finding(
                        "agent_disclosure_source_ref_invalid",
                        "A disclosure carries an invalid SourceRef",
                        entry_id=str(entry["id"]),
                    )
                )
                continue
            usage = sources.execute(
                "SELECT u.*,i.redaction_epoch current_redaction_epoch,"
                "r.content_sha256,r.byte_length,r.redacted_at "
                "FROM source_usage_intents u "
                "JOIN source_items i ON i.authority_id=u.authority_id "
                "AND i.source_item_id=u.source_item_id "
                "JOIN source_representations r ON r.representation_id=u.representation_id "
                "WHERE u.usage_id=?",
                (str(entry["reservation_id"]),),
            ).fetchone()
            if usage is None:
                findings.append(
                    _finding(
                        "agent_disclosure_reservation_missing",
                        "A disclosure reservation is absent from Sources",
                        entry_id=str(entry["id"]),
                        reservation_id=str(entry["reservation_id"]),
                    )
                )
                continue
            expected = (
                ref.authority_id,
                ref.item_id,
                str(entry["representation_id"]),
                str(entry["content_sha256"]),
                int(entry["byte_length"]),
                int(entry["redaction_epoch"]),
            )
            actual = (
                str(usage["authority_id"]),
                str(usage["source_item_id"]),
                str(usage["representation_id"]),
                str(usage["content_sha256"]),
                int(usage["byte_length"]),
                int(usage["bound_redaction_epoch"]),
            )
            if expected != actual or int(usage["current_redaction_epoch"]) != expected[-1]:
                findings.append(
                    _finding(
                        "agent_disclosure_reservation_mismatch",
                        "Agent Execution and Sources disagree about a disclosed boundary",
                        entry_id=str(entry["id"]),
                    )
                )
            if entry["source_acknowledgement"] == "acknowledged":
                expected_status = "acknowledged" if entry["state"] == "sent" else "released"
                if usage["status"] != expected_status:
                    findings.append(
                        _finding(
                            "agent_disclosure_ack_mismatch",
                            "Agent Execution and Sources disagree about a known outcome",
                            entry_id=str(entry["id"]),
                        )
                    )
            elif usage["status"] != "reserved":
                findings.append(
                    _finding(
                        "agent_disclosure_pending_reservation_mismatch",
                        "An unacknowledged disclosure no longer owns its exact reservation",
                        entry_id=str(entry["id"]),
                    )
                )
        pending_effects = sources.execute(
            "SELECT effect_id,status,target_domain,effect_type,payload_sha256,error_code "
            "FROM source_outbox "
            "WHERE status IN ('pending','leased','retryable','paused') "
            "ORDER BY created_at,effect_id"
        ).fetchall()
        reconciliation = restore_payload.get("reconciliation")
        deferred = (
            reconciliation.get("sources_effect_quarantine")
            if isinstance(reconciliation, Mapping)
            else None
        )
        deferred_by_id = dict(deferred) if isinstance(deferred, Mapping) else {}
        unsettled: list[sqlite3.Row] = []
        for effect in pending_effects:
            effect_id = str(effect["effect_id"])
            receipt = deferred_by_id.get(effect_id)
            valid_receipt = (
                isinstance(receipt, Mapping)
                and receipt.get("effect_id") == effect_id
                and receipt.get("status") == effect["status"]
                and receipt.get("target_domain") == effect["target_domain"]
                and receipt.get("effect_type") == effect["effect_type"]
                and receipt.get("payload_sha256") == effect["payload_sha256"]
                and effect["status"] == "paused"
                and effect["error_code"] == "imported_inert"
            )
            if not valid_receipt:
                unsettled.append(effect)
        if unsettled:
            findings.append(
                _finding(
                    "sources_effects_unsettled",
                    "Sources has unsettled effects that require domain-specific recovery",
                    effect_ids=[str(row["effect_id"]) for row in unsettled],
                )
            )
        unknown_deferred = set(deferred_by_id) - {
            str(row["effect_id"]) for row in pending_effects
        }
        if unknown_deferred:
            findings.append(
                _finding(
                    "sources_effect_quarantine_stale",
                    "A deferred Sources effect receipt no longer matches unsettled work",
                    effect_ids=sorted(unknown_deferred),
                )
            )
    except sqlite3.Error as exc:
        findings.append(
            _finding(
                "agent_sources_schema_mismatch",
                "Agent Execution and Sources cannot be compared",
                error=type(exc).__name__,
            )
        )
    finally:
        agent.close()
        sources.close()
    return findings


def _truth_causality_connections(
    paths: SourceFoundationPaths,
    store_id: str,
) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    """Open the one exact registered Truth/causality cohort read-only."""

    registry = sqlite3.connect(
        f"file:{paths.truth_registry_db.resolve()}?mode=ro", uri=True
    )
    try:
        rows = registry.execute(
            "SELECT path FROM truth_stores WHERE store_id=? AND reachable=1",
            (store_id,),
        ).fetchall()
    finally:
        registry.close()
    if len(rows) != 1:
        raise ValueError("registered_truth_store_unavailable")
    from work_buddy.truth.contracts import StorePaths

    sidecar = StorePaths.from_root(str(rows[0][0])).sidecar
    truth_path = sidecar / "store.db"
    causality_path = sidecar / "document-causality.db"
    if not truth_path.is_file() or not causality_path.is_file():
        raise ValueError("registered_truth_causality_unavailable")
    truth = sqlite3.connect(f"file:{truth_path.resolve()}?mode=ro", uri=True)
    causality = sqlite3.connect(
        f"file:{causality_path.resolve()}?mode=ro", uri=True
    )
    truth.row_factory = sqlite3.Row
    causality.row_factory = sqlite3.Row
    return truth, causality


def _current_document_head(truth: sqlite3.Connection, document_id: str) -> str | None:
    row = truth.execute(
        "SELECT structured_head_sha256 FROM document_versions "
        "WHERE document_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def _binding_row(
    causality: sqlite3.Connection,
    *,
    binding_id: str,
) -> sqlite3.Row | None:
    return causality.execute(
        "SELECT * FROM domain_document_bindings WHERE binding_id=?",
        (binding_id,),
    ).fetchone()


def _cursor_row(
    causality: sqlite3.Connection,
    *,
    binding_id: str,
) -> sqlite3.Row | None:
    return causality.execute(
        "SELECT * FROM document_projection_cursors WHERE binding_id=?",
        (binding_id,),
    ).fetchone()


def _journal_causality_findings(
    conn: sqlite3.Connection,
    paths: SourceFoundationPaths,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    conn.row_factory = sqlite3.Row
    migrations = conn.execute(
        "SELECT * FROM journal_content_migrations WHERE binding_id IS NOT NULL "
        "OR store_id IS NOT NULL OR document_id IS NOT NULL "
        "ORDER BY entity_kind,entity_id"
    ).fetchall()
    for migration in migrations:
        subject = f"{migration['entity_kind']}:{migration['entity_id']}"
        if not all(migration[key] for key in ("binding_id", "store_id", "document_id")):
            findings.append(
                _finding(
                    "journal_causality_binding_incomplete",
                    "A Journal migration has incomplete Truth binding coordinates",
                    subject=subject,
                )
            )
            continue
        truth = causality = None
        try:
            truth, causality = _truth_causality_connections(
                paths, str(migration["store_id"])
            )
            binding = _binding_row(
                causality, binding_id=str(migration["binding_id"])
            )
            state = str(migration["mirrored_state"])
            expected_authority = (
                "co_work"
                if state in {"cowork_authoritative", "paused_diverged"}
                else "domain"
                if state in {"shadow_imported", "legacy_authoritative"}
                else None
            )
            retired = state == "retired"
            valid = (
                binding is not None
                and str(binding["store_id"]) == str(migration["store_id"])
                and str(binding["document_id"]) == str(migration["document_id"])
                and str(binding["domain_namespace"]) == "journal"
                and str(binding["domain_kind"]) == str(migration["entity_kind"])
                and str(binding["domain_entity_id"]) == str(migration["marker_id"])
                and str(binding["role"]) == str(migration["entity_kind"])
                and int(binding["content_authority_epoch"])
                == int(migration["mirrored_authority_epoch"])
                and str(binding["lifecycle"])
                == ("retired" if retired else "current")
                and (
                    retired
                    or (
                        expected_authority is not None
                        and str(binding["content_authority"]) == expected_authority
                    )
                )
            )
            if not valid:
                raise ValueError("journal_causality_binding_mismatch")
            projection_state = str(migration["projection_state"])
            if projection_state in {"pending", "failed"}:
                findings.append(
                    _finding(
                        "journal_projection_attention_required",
                        "A Journal migration projection has not reached a recoverable terminal state",
                        subject=subject,
                        projection_state=projection_state,
                    )
                )
            if expected_authority == "co_work" and not retired:
                cursor = _cursor_row(
                    causality, binding_id=str(migration["binding_id"])
                )
                expected_cursor = (
                    "committed"
                    if projection_state == "committed"
                    else "paused_diverged"
                    if projection_state == "paused_diverged"
                    else None
                )
                if (
                    cursor is None
                    or int(cursor["content_authority_epoch"])
                    != int(migration["mirrored_authority_epoch"])
                    or expected_cursor is None
                    or str(cursor["status"]) != expected_cursor
                    or (
                        expected_cursor == "paused_diverged"
                        and str(cursor["divergence_source_ref"] or "")
                        != str(migration["divergence_source_ref"] or "")
                    )
                ):
                    raise ValueError("journal_causality_cursor_mismatch")
        except (sqlite3.Error, TypeError, ValueError):
            findings.append(
                _finding(
                    "journal_causality_mismatch",
                    "Journal migration authority does not match portable document causality",
                    subject=subject,
                )
            )
        finally:
            if truth is not None:
                truth.close()
            if causality is not None:
                causality.close()

    pilot_rows = conn.execute(
        "SELECT * FROM journal_document_bindings ORDER BY entry_id"
    ).fetchall()
    for pilot in pilot_rows:
        subject = str(pilot["entry_id"])
        truth = causality = None
        try:
            truth, causality = _truth_causality_connections(
                paths, str(pilot["store_id"])
            )
            binding = _binding_row(causality, binding_id=str(pilot["binding_id"]))
            cursor = _cursor_row(causality, binding_id=str(pilot["binding_id"]))
            expected_cursor = (
                "committed"
                if pilot["state"] == "current"
                else "paused_diverged"
                if pilot["state"] == "paused_diverged"
                else None
            )
            retired = pilot["state"] == "retired"
            if (
                binding is None
                or str(binding["store_id"]) != str(pilot["store_id"])
                or str(binding["document_id"]) != str(pilot["document_id"])
                or str(binding["domain_namespace"]) != "journal"
                or str(binding["domain_kind"]) != "running_note"
                or str(binding["domain_entity_id"]) != subject
                or str(binding["role"]) != "running_note"
                or (not retired and str(binding["content_authority"]) != "co_work")
                or int(binding["content_authority_epoch"])
                != int(pilot["content_authority_epoch"])
                or str(binding["lifecycle"])
                != ("retired" if retired else "current")
                or (
                    not retired
                    and (
                        expected_cursor is None
                        or cursor is None
                        or int(cursor["content_authority_epoch"])
                        != int(pilot["content_authority_epoch"])
                        or str(cursor["status"]) != expected_cursor
                        or (
                            expected_cursor == "paused_diverged"
                            and not cursor["divergence_source_ref"]
                        )
                    )
                )
            ):
                raise ValueError("journal_pilot_causality_mismatch")
        except (sqlite3.Error, TypeError, ValueError):
            findings.append(
                _finding(
                    "journal_pilot_causality_mismatch",
                    "A Journal pilot binding does not match portable document causality",
                    entry_id=subject,
                )
            )
        finally:
            if truth is not None:
                truth.close()
            if causality is not None:
                causality.close()
    return findings


def _inspect_journal(paths: SourceFoundationPaths) -> list[dict[str, Any]]:
    findings = _sqlite_integrity(paths.journal_capture_db, label="journal_capture")
    if findings:
        return findings
    conn = sqlite3.connect(
        f"file:{paths.journal_capture_db.resolve()}?mode=ro", uri=True
    )
    try:
        from work_buddy.journal_capture.store import _SCHEMA_VERSION

        meta = conn.execute(
            "SELECT value FROM journal_meta WHERE key='schema_version'"
        ).fetchall()
        if len(meta) != 1 or int(meta[0][0]) != _SCHEMA_VERSION:
            raise sqlite3.DatabaseError("journal capture schema version mismatch")
        unsettled = {
            "effects": int(
                conn.execute(
                    "SELECT COUNT(*) FROM journal_effects "
                    "WHERE state IN ('pending','running','paused')"
                ).fetchone()[0]
            ),
            "projections": int(
                conn.execute(
                    "SELECT COUNT(*) FROM journal_entries "
                    "WHERE projection_state != 'committed'"
                ).fetchone()[0]
            ),
            "migrations": int(
                conn.execute(
                    "SELECT COUNT(*) FROM journal_migration_operations "
                    "WHERE state != 'completed'"
                ).fetchone()[0]
            ),
            "usage_transitions": int(
                conn.execute(
                    "SELECT COUNT(*) FROM journal_document_usage_transitions "
                    "WHERE state != 'complete'"
                ).fetchone()[0]
            ),
        }
        if any(unsettled.values()):
            findings.append(
                _finding(
                    "journal_operations_unsettled",
                    "Journal has effects, projections, migrations, or source-usage transitions requiring recovery",
                    **unsettled,
                )
            )
        findings.extend(_journal_causality_findings(conn, paths))
    except sqlite3.Error as exc:
        findings.append(
            _finding(
                "journal_capture_schema_mismatch",
                "Journal capture recovery state cannot be inspected",
                error=type(exc).__name__,
            )
        )
    finally:
        conn.close()
    return findings


def _inspect_conversation_dependencies(
    paths: SourceFoundationPaths,
) -> list[dict[str, Any]]:
    dependency_path = paths.cowork_conversation_source_dependencies_db
    findings = _sqlite_integrity(
        dependency_path,
        label="cowork_conversation_source_dependencies",
    )
    findings.extend(_sqlite_integrity(paths.conversations_db, label="conversations"))
    if findings:
        return findings
    dependencies = sqlite3.connect(
        f"file:{dependency_path.resolve()}?mode=ro", uri=True
    )
    conversations = sqlite3.connect(
        f"file:{paths.conversations_db.resolve()}?mode=ro", uri=True
    )
    dependencies.row_factory = sqlite3.Row
    conversations.row_factory = sqlite3.Row
    try:
        from work_buddy.cowork.conversation_source_dependencies import (
            _REDACTION_REPLACEMENT,
            _SCHEMA_VERSION,
        )

        versions = dependencies.execute(
            "SELECT value FROM cowork_conversation_source_dependency_meta "
            "WHERE key='schema_version'"
        ).fetchall()
        if len(versions) != 1 or int(versions[0][0]) != _SCHEMA_VERSION:
            raise sqlite3.DatabaseError("conversation dependency schema mismatch")
        rows = dependencies.execute(
            "SELECT * FROM cowork_conversation_source_dependencies "
            "ORDER BY created_at,dependency_id"
        ).fetchall()
        dependency_by_message = {str(row["message_id"]): row for row in rows}
        cowork_messages: dict[str, tuple[sqlite3.Row, str, str]] = {}
        conversation_rows = conversations.execute(
            "SELECT conversation_id,source,metadata FROM conversations "
            "WHERE source='cowork_document' ORDER BY conversation_id"
        ).fetchall()
        for conversation in conversation_rows:
            conversation_id = str(conversation["conversation_id"])
            try:
                metadata = json.loads(str(conversation["metadata"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = None
            if not isinstance(metadata, dict):
                findings.append(
                    _finding(
                        "cowork_conversation_dependency_owner_invalid",
                        "A Co-work conversation has invalid document ownership metadata",
                        conversation_id=conversation_id,
                    )
                )
                continue
            store_id = metadata.get("cowork_store_id")
            document_id = metadata.get("cowork_document_id")
            kind = metadata.get("cowork_kind")
            if (
                not isinstance(store_id, str)
                or not store_id
                or not isinstance(document_id, str)
                or not document_id
                or kind != "document_conversation"
            ):
                findings.append(
                    _finding(
                        "cowork_conversation_dependency_owner_invalid",
                        "A Co-work conversation has invalid document ownership metadata",
                        conversation_id=conversation_id,
                    )
                )
                continue
            for message in conversations.execute(
                "SELECT message_id,conversation_id,role,content FROM messages "
                "WHERE conversation_id=? AND role IN ('user','agent') "
                "ORDER BY created_at,message_id",
                (conversation_id,),
            ).fetchall():
                message_id = str(message["message_id"])
                cowork_messages[message_id] = (message, store_id, document_id)
                dependency = dependency_by_message.get(message_id)
                if dependency is None:
                    findings.append(
                        _finding(
                            "cowork_conversation_dependency_missing",
                            "A retained Co-work user or agent message has no dependency receipt",
                            message_id=message_id,
                            conversation_id=conversation_id,
                            store_id=store_id,
                            document_id=document_id,
                            role=str(message["role"]),
                        )
                    )
        scrubbed_digest = _sha256_bytes(_REDACTION_REPLACEMENT.encode("utf-8"))
        for row in rows:
            dependency_id = str(row["dependency_id"])
            content_digest = str(row["content_sha256"])
            input_digest = row["input_manifest_sha256"]
            if (
                len(content_digest) != 64
                or any(character not in "0123456789abcdef" for character in content_digest)
                or (
                    input_digest is not None
                    and (
                        len(str(input_digest)) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in str(input_digest)
                        )
                    )
                )
            ):
                findings.append(
                    _finding(
                        "cowork_conversation_dependency_digest_invalid",
                        "A Co-work conversation dependency has an invalid digest boundary",
                        dependency_id=dependency_id,
                    )
                )
                continue
            message = conversations.execute(
                "SELECT m.conversation_id,m.role,m.content,c.source,c.metadata "
                "FROM messages m JOIN conversations c "
                "ON c.conversation_id=m.conversation_id WHERE m.message_id=?",
                (str(row["message_id"]),),
            ).fetchone()
            owner = cowork_messages.get(str(row["message_id"]))
            if (
                message is None
                or str(message["source"]) != "cowork_document"
                or owner is None
                or str(message["conversation_id"]) != str(row["conversation_id"])
                or str(message["role"]) != str(row["role"])
                or owner[1] != str(row["store_id"])
                or owner[2] != str(row["document_id"])
            ):
                findings.append(
                    _finding(
                        "cowork_conversation_dependency_message_mismatch",
                        "A Co-work conversation dependency cannot be matched to its retained message",
                        dependency_id=dependency_id,
                    )
                )
                continue
            live_digest = _sha256_bytes(str(message["content"]).encode("utf-8"))
            state = str(row["state"])
            if state == "active" and live_digest != content_digest:
                findings.append(
                    _finding(
                        "cowork_conversation_dependency_content_mismatch",
                        "An active Co-work conversation dependency no longer matches its retained message",
                        dependency_id=dependency_id,
                    )
                )
            elif state == "scrubbed" and live_digest != scrubbed_digest:
                findings.append(
                    _finding(
                        "cowork_conversation_dependency_scrub_mismatch",
                        "A settled Co-work conversation dependency lacks the scrubbed message receipt",
                        dependency_id=dependency_id,
                    )
                )
            elif state == "review_required":
                findings.append(
                    _finding(
                        "cowork_conversation_dependency_review_required",
                        "A semantic or unclassifiable Co-work conversation dependency still needs review",
                        dependency_id=dependency_id,
                    )
                )
    except (sqlite3.Error, ValueError) as exc:
        findings.append(
            _finding(
                "cowork_conversation_dependency_schema_mismatch",
                "Co-work conversation dependency state cannot be reconciled",
                error=type(exc).__name__,
            )
        )
    finally:
        dependencies.close()
        conversations.close()
    return findings


def _inspect_task_notes(paths: SourceFoundationPaths) -> list[dict[str, Any]]:
    findings = _sqlite_integrity(paths.task_note_migration_db, label="task_note_migration")
    if findings:
        return findings
    conn = sqlite3.connect(
        f"file:{paths.task_note_migration_db.resolve()}?mode=ro", uri=True
    )
    try:
        from work_buddy.task_notes.store import SCHEMA_VERSION

        versions = conn.execute(
            "SELECT value FROM migration_meta WHERE key='schema_version'"
        ).fetchall()
        if len(versions) != 1 or int(versions[0][0]) != SCHEMA_VERSION:
            raise sqlite3.DatabaseError("task-note migration schema mismatch")
        unsettled = 0
        for table, column, terminal in (
            ("task_note_sagas", "state", ("completed",)),
            ("task_note_change_operations", "state", ("completed",)),
            ("task_note_source_dependencies", "state", ("acknowledged", "released")),
        ):
            marks = ",".join("?" for _ in terminal)
            unsettled += int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} NOT IN ({marks})",
                    terminal,
                ).fetchone()[0]
            )
        if unsettled:
            findings.append(
                _finding(
                    "task_note_operations_unsettled",
                    "Task-note migration has operations requiring recovery or review",
                    count=unsettled,
                )
            )
        conn.row_factory = sqlite3.Row
        migrations = conn.execute(
            "SELECT m.*,e.state authority_state,e.epoch authority_epoch,"
            "e.domain_revision authority_domain_revision,e.rollback_deadline "
            "FROM task_note_migrations m LEFT JOIN content_authority_epochs e "
            "ON e.domain_namespace='tasks' AND e.entity_kind='task_note' "
            "AND e.entity_id=m.note_uuid "
            "WHERE m.binding_id IS NOT NULL OR m.store_id IS NOT NULL "
            "OR m.document_id IS NOT NULL ORDER BY m.note_uuid"
        ).fetchall()
        for migration in migrations:
            note_uuid = str(migration["note_uuid"])
            truth = causality = None
            try:
                if not all(
                    migration[key] for key in ("binding_id", "store_id", "document_id")
                ):
                    raise ValueError("task_note_binding_incomplete")
                truth, causality = _truth_causality_connections(
                    paths, str(migration["store_id"])
                )
                binding = _binding_row(
                    causality, binding_id=str(migration["binding_id"])
                )
                authority_state = str(migration["authority_state"] or "")
                expected_authority = (
                    "co_work"
                    if authority_state == "cowork_authoritative"
                    else "domain"
                    if authority_state in {"shadow_imported", "legacy_authoritative"}
                    else None
                )
                expected_lifecycle = (
                    "retired" if authority_state == "retired" else "current"
                )
                if (
                    binding is None
                    or str(binding["store_id"]) != str(migration["store_id"])
                    or str(binding["document_id"]) != str(migration["document_id"])
                    or str(binding["domain_namespace"]) != "tasks"
                    or str(binding["domain_kind"]) != "task_note"
                    or str(binding["domain_entity_id"]) != note_uuid
                    or str(binding["role"]) != "task_note"
                    or int(binding["content_authority_epoch"])
                    != int(migration["authority_epoch"])
                    or str(binding["domain_revision"])
                    != str(migration["authority_domain_revision"])
                    or str(binding["lifecycle"]) != expected_lifecycle
                    or (
                        authority_state != "retired"
                        and (
                            expected_authority is None
                            or str(binding["content_authority"])
                            != expected_authority
                        )
                    )
                    or (
                        authority_state == "cowork_authoritative"
                        and not migration["rollback_deadline"]
                    )
                ):
                    raise ValueError("task_note_causality_binding_mismatch")
                projection_state = str(migration["projection_state"])
                current_head = _current_document_head(
                    truth, str(migration["document_id"])
                )
                if projection_state == "current" and (
                    not migration["projection_document_head"]
                    or str(migration["projection_document_head"]) != current_head
                    or not migration["projection_result_sha256"]
                    or int(migration["projection_generation"]) < 1
                ):
                    raise ValueError("task_note_projection_head_mismatch")
                if projection_state == "paused_diverged" and not migration[
                    "divergence_source_ref"
                ]:
                    raise ValueError("task_note_divergence_receipt_missing")
            except (sqlite3.Error, TypeError, ValueError):
                findings.append(
                    _finding(
                        "task_note_causality_mismatch",
                        "Task-note migration authority does not match portable document causality",
                        note_uuid=note_uuid,
                    )
                )
            finally:
                if truth is not None:
                    truth.close()
                if causality is not None:
                    causality.close()
    except sqlite3.Error as exc:
        findings.append(
            _finding(
                "task_note_schema_mismatch",
                "Task-note migration state cannot be reconciled",
                error=type(exc).__name__,
            )
        )
    finally:
        conn.close()
    return findings


def _inspect_truth_stores(
    paths: SourceFoundationPaths,
    restore_payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    findings = _sqlite_integrity(paths.truth_registry_db, label="truth_registry")
    if findings:
        return findings
    registry = sqlite3.connect(
        f"file:{paths.truth_registry_db.resolve()}?mode=ro", uri=True
    )
    registry.row_factory = sqlite3.Row
    try:
        rows = registry.execute(
            "SELECT path,store_id,reachable FROM truth_stores ORDER BY path"
        ).fetchall()
    except sqlite3.Error as exc:
        registry.close()
        return [
            _finding(
                "truth_registry_schema_mismatch",
                "Registered Truth stores cannot be enumerated",
                error=type(exc).__name__,
            )
        ]
    finally:
        try:
            registry.close()
        except Exception:
            pass
    expected_rows = restore_payload.get("truth_stores")
    if not isinstance(expected_rows, list):
        findings.append(
            _finding(
                "truth_restore_inventory_missing",
                "The restore marker has no scoped Truth inventory",
            )
        )
        expected_rows = []
    reconciliation = restore_payload.get("reconciliation")
    quarantined = (
        reconciliation.get("truth_quarantine")
        if isinstance(reconciliation, Mapping)
        else None
    )
    quarantined_by_id = dict(quarantined) if isinstance(quarantined, Mapping) else {}
    expected_ids: set[str] = set()
    inventory_ids: set[str] = set()
    for item in expected_rows:
        if not isinstance(item, Mapping) or not isinstance(item.get("store_id"), str):
            findings.append(
                _finding(
                    "truth_restore_inventory_invalid",
                    "The restore marker contains an invalid scoped Truth entry",
                )
            )
            continue
        store_id = str(item["store_id"])
        if store_id in inventory_ids:
            findings.append(
                _finding(
                    "truth_restore_inventory_duplicate",
                    "The restore marker repeats a scoped Truth identity",
                    store_id=store_id,
                )
            )
        inventory_ids.add(store_id)
        quarantine_receipt = quarantined_by_id.get(store_id)
        if quarantine_receipt is not None:
            if (
                not isinstance(quarantine_receipt, Mapping)
                or quarantine_receipt.get("store_id") != store_id
                or quarantine_receipt.get("backup_status") != item.get("backup_status")
                or quarantine_receipt.get("inventory_sha256")
                != _sha256_json(dict(item))
            ):
                findings.append(
                    _finding(
                        "truth_restore_quarantine_invalid",
                        "A scoped Truth quarantine receipt does not match its frozen inventory",
                        store_id=store_id,
                    )
                )
            continue
        expected_ids.add(store_id)
        if item.get("backup_status") == "included":
            digests = (
                item.get("export_sha256"),
                item.get("causality_sha256"),
                item.get("causality_payload_sha256"),
            )
            if any(
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                for digest in digests
            ):
                findings.append(
                    _finding(
                        "truth_restore_companion_digest_invalid",
                        "A backed-up Truth store lacks complete ledger/causality digests",
                        store_id=store_id,
                    )
                )
    unknown_quarantine = set(quarantined_by_id) - inventory_ids
    if unknown_quarantine:
        findings.append(
            _finding(
                "truth_restore_quarantine_invalid",
                "A scoped Truth quarantine receipt names an unknown store identity",
                store_ids=sorted(unknown_quarantine),
            )
        )
    registered_ids = {str(row["store_id"]) for row in rows}
    if registered_ids != expected_ids:
        findings.append(
            _finding(
                "truth_restore_inventory_mismatch",
                "The restored registry and snapshot Truth inventory disagree",
                expected_store_ids=sorted(expected_ids),
                registered_store_ids=sorted(registered_ids),
            )
        )
    from work_buddy.truth.contracts import StorePaths

    for row in rows:
        store_id = str(row["store_id"])
        if store_id not in expected_ids:
            continue
        sidecar = StorePaths.from_root(str(row["path"])).sidecar
        truth_db = sidecar / "store.db"
        store_findings = _sqlite_integrity(truth_db, label="truth_store")
        if store_findings:
            findings.extend(
                {**item, "context": {**item["context"], "store_id": store_id}}
                for item in store_findings
            )
            continue
        conn = sqlite3.connect(f"file:{truth_db.resolve()}?mode=ro", uri=True)
        try:
            info = conn.execute("SELECT store_id FROM store_info").fetchall()
            document_ids = {str(item[0]) for item in conn.execute("SELECT id FROM documents")}
            hindsight_unsettled = conn.execute(
                "SELECT effect_id,state FROM truth_hindsight_projection_outbox "
                "WHERE state NOT IN ('delivered','superseded') "
                "ORDER BY created_at,effect_id"
            ).fetchall()
            hindsight_cleanup = conn.execute(
                "SELECT cleanup_id FROM truth_hindsight_projection_source_cleanup "
                "WHERE state='pending' ORDER BY created_at,cleanup_id"
            ).fetchall()
        finally:
            conn.close()
        if len(info) != 1 or str(info[0][0]) != store_id:
            findings.append(
                _finding(
                    "truth_store_identity_mismatch",
                    "The registry and scoped Truth store disagree about permanent identity",
                    store_id=store_id,
                )
            )
            continue
        if hindsight_unsettled or hindsight_cleanup:
            findings.append(
                _finding(
                    "truth_hindsight_projection_unsettled",
                    "A Truth store has Hindsight projection delivery or cleanup requiring recovery",
                    store_id=store_id,
                    effect_ids=[str(item[0]) for item in hindsight_unsettled],
                    cleanup_ids=[str(item[0]) for item in hindsight_cleanup],
                )
            )
        causality_path = sidecar / "document-causality.db"
        if not causality_path.is_file():
            findings.append(
                _finding(
                    "truth_store_causality_missing",
                    "A scoped Truth store has no document causality cohort",
                    store_id=store_id,
                )
            )
            continue
        try:
            from work_buddy.document_kernel.causality import DocumentCausalityStore

            causality = DocumentCausalityStore(sidecar)
            bundle = causality.export_recovery_bundle(store_id=store_id)
            DocumentCausalityStore.validate_recovery_bundle(
                bundle,
                expected_store_id=store_id,
                expected_document_ids=document_ids,
            )
            if causality.incomplete_changes():
                findings.append(
                    _finding(
                        "truth_store_changes_incomplete",
                        "A scoped Truth store has incomplete document changes",
                        store_id=store_id,
                    )
                )
        except Exception as exc:
            findings.append(
                _finding(
                    "truth_store_causality_invalid",
                    "A scoped Truth store's causality cohort cannot be validated",
                    store_id=store_id,
                    error=type(exc).__name__,
                )
            )
    return findings


def _portable_truth_member(
    marker: Path,
    payload: Mapping[str, Any],
    member: object,
) -> Path:
    root_member = payload.get("portable_truth_root")
    if not isinstance(root_member, str) or not root_member:
        raise ValueError("portable_truth_recovery_root_missing")
    relative_root = Path(root_member)
    if relative_root.is_absolute() or ".." in relative_root.parts:
        raise ValueError("portable_truth_recovery_root_invalid")
    root = (marker.parent / relative_root).resolve()
    try:
        root.relative_to(marker.parent.resolve())
    except ValueError as exc:
        raise ValueError("portable_truth_recovery_root_invalid") from exc
    if not isinstance(member, str) or not member:
        raise ValueError("portable_truth_recovery_member_missing")
    relative_member = Path(member)
    if (
        relative_member.is_absolute()
        or ".." in relative_member.parts
        or not relative_member.parts
        or relative_member.parts[0] != "truth_stores"
    ):
        raise ValueError("portable_truth_recovery_member_invalid")
    candidate = (root / Path(*relative_member.parts[1:])).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("portable_truth_recovery_member_invalid") from exc
    if not candidate.is_file():
        raise ValueError("portable_truth_recovery_member_missing")
    return candidate


def reconcile_portable_truth_stores(
    *,
    marker_path: str | Path,
    paths: SourceFoundationPaths,
    recovery_targets: Mapping[str, str] | None = None,
    quarantine_store_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Recover or explicitly quarantine frozen scoped Truth inventory.

    Callers must hold the high-consent restore operator boundary and the
    authorized reconciliation context.  Portable payloads are never inferred
    into a location: every target root and every quarantined permanent identity
    is named in the approved scope.
    """

    fence = read_restore_fence(marker_path)
    if not fence.active or not fence.valid or fence.payload is None:
        raise ValueError(fence.error or "source_foundation_restore_fence_unavailable")
    payload = fence.payload
    rows = payload.get("truth_stores")
    if not isinstance(rows, list):
        raise ValueError("truth_restore_inventory_missing")
    inventory: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("store_id"), str):
            raise ValueError("truth_restore_inventory_invalid")
        store_id = str(row["store_id"])
        if store_id in inventory:
            raise ValueError("truth_restore_inventory_duplicate")
        inventory[store_id] = row
    targets = {str(key): str(value) for key, value in (recovery_targets or {}).items()}
    quarantine_ids = tuple(sorted(set(quarantine_store_ids)))
    if set(targets) & set(quarantine_ids):
        raise ValueError("truth_restore_action_conflict")
    unknown = (set(targets) | set(quarantine_ids)) - set(inventory)
    if unknown:
        raise ValueError("truth_restore_unknown_store")

    from work_buddy.document_kernel.causality import DocumentCausalityStore
    from work_buddy.truth.contracts import StorePaths
    from work_buddy.truth.export import export_store, import_store
    from work_buddy.truth.registry import TruthStoreRegistry
    from work_buddy.truth.store import TruthStore

    registry = TruthStoreRegistry(paths.truth_registry_db)
    recovered: dict[str, dict[str, Any]] = {}
    quarantined: dict[str, dict[str, Any]] = {}

    for store_id in quarantine_ids:
        item = inventory[store_id]
        for registered in registry.list_stores(refresh=False):
            if registered.store_id == store_id:
                registry.unregister(registered.path)
        quarantined[store_id] = {
            "store_id": store_id,
            "backup_status": item.get("backup_status"),
            "inventory_sha256": _sha256_json(dict(item)),
            "quarantined_at": _now(),
            "portable_payload_retained": item.get("backup_status") == "included",
        }

    class _ReachableOnlyRegistry:
        def paths_for_store_id(self, expected_store_id: str) -> tuple[Path, ...]:
            live: list[Path] = []
            for registered in registry.list_stores(refresh=False):
                if registered.store_id != expected_store_id:
                    continue
                try:
                    observed = TruthStore.open(registered.path)
                except Exception:
                    continue
                if observed.store_id == expected_store_id:
                    live.append(observed.paths.sidecar)
            return tuple(live)

    for store_id, target_value in sorted(targets.items()):
        item = inventory[store_id]
        if item.get("backup_status") != "included":
            raise ValueError("truth_restore_payload_unavailable")
        export_path = _portable_truth_member(
            fence.path, payload, item.get("export_member")
        )
        causality_path = _portable_truth_member(
            fence.path, payload, item.get("causality_member")
        )
        export_sha = str(item.get("export_sha256") or "")
        causality_sha = str(item.get("causality_sha256") or "")
        if _sha256_bytes(export_path.read_bytes()) != export_sha:
            raise ValueError("truth_restore_export_digest_mismatch")
        if _sha256_bytes(causality_path.read_bytes()) != causality_sha:
            raise ValueError("truth_restore_causality_digest_mismatch")
        target = Path(target_value).expanduser().resolve()
        if not target.is_dir():
            raise ValueError("truth_restore_target_must_exist")
        target_paths = StorePaths.from_root(target)
        if target_paths.db.is_file():
            restored = TruthStore.open(target_paths.sidecar)
            if restored.store_id != store_id:
                raise ValueError("truth_restore_target_identity_mismatch")
            reproduced = export_store(restored)
            if reproduced.sha256 != export_sha:
                raise ValueError("truth_restore_target_ledger_mismatch")
            causality = DocumentCausalityStore(restored.paths.sidecar)
            causality_bundle = causality.export_recovery_bundle(store_id=store_id)
            if causality_bundle.get("payload_sha256") != item.get(
                "causality_payload_sha256"
            ):
                raise ValueError("truth_restore_target_causality_mismatch")
        else:
            result = import_store(
                export_path,
                target,
                registry=_ReachableOnlyRegistry(),
                causality_source=causality_path,
                causality_sha256=causality_sha,
            )
            restored = result.store
        for registered in registry.list_stores(refresh=False):
            if (
                registered.store_id == store_id
                and registered.path.resolve() != restored.paths.sidecar.resolve()
            ):
                registry.unregister(registered.path)
        registry.register(restored)
        recovered[store_id] = {
            "store_id": store_id,
            "target": str(target),
            "export_sha256": export_sha,
            "causality_sha256": causality_sha,
            "causality_payload_sha256": item.get("causality_payload_sha256"),
            "recovered_at": _now(),
        }

    if recovered or quarantined:
        updated = dict(payload)
        reconciliation = dict(updated.get("reconciliation") or {})
        prior_recovered = dict(reconciliation.get("truth_recovery") or {})
        prior_quarantine = dict(reconciliation.get("truth_quarantine") or {})
        prior_recovered.update(recovered)
        prior_quarantine.update(quarantined)
        reconciliation["truth_recovery"] = prior_recovered
        reconciliation["truth_quarantine"] = prior_quarantine
        updated["reconciliation"] = reconciliation
        write_restore_fence(updated, path=fence.path)
    return {"recovered": recovered, "quarantined": quarantined}


def reconstitute_sources_from_archive(
    archive_path: str | Path,
    *,
    paths: SourceFoundationPaths,
    principal: Any,
    authorization_fingerprint: str,
    expected_sha256: str,
) -> dict[str, Any]:
    """Rebuild a missing Sources authority from one exact authorized archive."""

    archive = Path(archive_path).expanduser().resolve()
    try:
        payload = archive.read_bytes()
        if _sha256_bytes(payload) != expected_sha256:
            raise ValueError("sources_restore_archive_digest_mismatch")
        first_line = next(line for line in payload.decode("utf-8").splitlines() if line)
        manifest = json.loads(first_line)
    except (OSError, StopIteration, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("sources_restore_archive_invalid") from exc
    from work_buddy.sources.models import EXPORT_SCHEMA, SourceRef

    if not isinstance(manifest, dict) or manifest.get("schema") != EXPORT_SCHEMA:
        raise ValueError("sources_restore_archive_invalid")
    authority_id = manifest.get("exporting_authority_id")
    if not isinstance(authority_id, str):
        raise ValueError("sources_restore_archive_invalid")
    SourceRef(authority_id, "restore-probe")
    target = paths.sources_root.expanduser().resolve()
    if target.exists():
        raise ValueError("sources_reconstitution_requires_missing_target")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.reconstituting-",
            dir=target.parent,
        )
    )
    try:
        from work_buddy.sources.export import ImportAuthorization, import_sources
        from work_buddy.sources.store import SourceStore

        store = SourceStore.create(temporary, authority_id=authority_id)
        result = import_sources(
            store,
            archive,
            authorization=ImportAuthorization(
                principal=principal,
                authorization_fingerprint=authorization_fingerprint,
                allow_foreign_authorities=False,
                collision_policy="reject",
                restore_operational_state=True,
            ),
        )
        if target.exists():
            raise ValueError("sources_reconstitution_target_changed")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            import shutil

            shutil.rmtree(temporary)
    return {
        "schema": "wb.source-foundation-sources-reconstitution/v1",
        "archive": str(archive),
        "archive_sha256": _sha256_bytes(payload),
        "authority_id": authority_id,
        "item_count": result.item_count,
        "quarantined_count": result.quarantined_count,
        "reconstituted_at": _now(),
        "operational_effects_paused": True,
    }


def quarantine_and_reconstitute_missing_cohorts(
    cohort_names: tuple[str, ...],
    *,
    marker_path: str | Path,
    paths: SourceFoundationPaths,
) -> dict[str, Any]:
    """Converge explicitly abandoned, non-portable state without guessing.

    Journal capture can restart empty only when its authority database is
    absent.  Co-work conversation recovery can restart empty only when the
    conversation database is absent; any retained dependency ledger is hot-
    copied into the fenced recovery area before an empty ledger is published.
    Existing-but-incoherent cohorts are never rewritten by this path.
    """

    allowed = {"journal_capture", "cowork_conversations"}
    names = tuple(sorted(set(cohort_names)))
    if set(names) - allowed:
        raise ValueError("unsupported_missing_cohort_quarantine")
    fence = read_restore_fence(marker_path)
    if not fence.active or not fence.valid or fence.payload is None:
        raise ValueError(fence.error or "source_foundation_restore_fence_unavailable")
    receipts: dict[str, dict[str, Any]] = {}
    recovery_root = (
        fence.path.parent
        / "source_foundation_recovery"
        / str(fence.payload["snapshot_id"])
        / "quarantine"
    )

    if "journal_capture" in names and not paths.journal_capture_db.is_file():
        from work_buddy.journal_capture.store import JournalCaptureStore

        JournalCaptureStore(paths.journal_capture_db)
        receipts["journal_capture"] = {
            "reason": "authority_database_absent",
            "disposition": "empty_domain_state_unknown_provenance",
            "reconstituted_at": _now(),
        }

    if "cowork_conversations" in names and not paths.conversations_db.is_file():
        recovery_root.mkdir(parents=True, exist_ok=True)
        dependency_receipt: dict[str, Any] | None = None
        if paths.cowork_conversation_source_dependencies_db.is_file():
            destination = recovery_root / "cowork-conversation-source-dependencies.db"
            if not destination.exists():
                source = sqlite3.connect(
                    f"file:{paths.cowork_conversation_source_dependencies_db.resolve()}?mode=ro",
                    uri=True,
                )
                target = sqlite3.connect(destination)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
            dependency_receipt = {
                "path": str(destination),
                "sha256": _sha256_bytes(destination.read_bytes()),
            }
        paths.conversations_db.parent.mkdir(parents=True, exist_ok=True)
        temporary_conversations = paths.conversations_db.with_name(
            f".{paths.conversations_db.name}.reconstituting"
        )
        if temporary_conversations.exists():
            temporary_conversations.unlink()
        conn = sqlite3.connect(temporary_conversations)
        try:
            from work_buddy.conversations.store import _ensure_schema

            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            conn.commit()
        finally:
            conn.close()
        os.replace(temporary_conversations, paths.conversations_db)

        dependency_path = paths.cowork_conversation_source_dependencies_db
        temporary_dependency = dependency_path.with_name(
            f".{dependency_path.name}.reconstituting"
        )
        if temporary_dependency.exists():
            temporary_dependency.unlink()
        from work_buddy.cowork.conversation_source_dependencies import _connect

        empty = _connect(temporary_dependency, write=True)
        empty.close()
        os.replace(temporary_dependency, dependency_path)
        receipts["cowork_conversations"] = {
            "reason": "conversation_database_absent",
            "disposition": "empty_conversation_and_dependency_authorities",
            "dependency_quarantine": dependency_receipt,
            "reconstituted_at": _now(),
        }

    if receipts:
        updated = dict(fence.payload)
        reconciliation = dict(updated.get("reconciliation") or {})
        prior = dict(reconciliation.get("missing_cohort_quarantine") or {})
        prior.update(receipts)
        reconciliation["missing_cohort_quarantine"] = prior
        updated["reconciliation"] = reconciliation
        write_restore_fence(updated, path=fence.path)
    return receipts


def quarantine_imported_source_effects(
    effect_ids: tuple[str, ...],
    *,
    marker_path: str | Path,
    paths: SourceFoundationPaths,
) -> dict[str, Any]:
    """Defer exact inert imported effects until the central fence is clear."""

    ids = tuple(sorted(set(effect_ids)))
    if not ids:
        return {}
    fence = read_restore_fence(marker_path)
    if not fence.active or not fence.valid or fence.payload is None:
        raise ValueError(fence.error or "source_foundation_restore_fence_unavailable")
    store_db = paths.sources_root / "store.db"
    conn = sqlite3.connect(f"file:{store_db.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT effect_id,status,target_domain,effect_type,payload_sha256,error_code "
            "FROM source_outbox WHERE effect_id IN ("
            + ",".join("?" for _ in ids)
            + ") ORDER BY effect_id",
            ids,
        ).fetchall()
    finally:
        conn.close()
    if {str(row["effect_id"]) for row in rows} != set(ids):
        raise ValueError("sources_effect_quarantine_unknown")
    receipts: dict[str, Any] = {}
    for row in rows:
        if row["status"] != "paused" or row["error_code"] != "imported_inert":
            raise ValueError("sources_effect_quarantine_requires_imported_inert")
        effect_id = str(row["effect_id"])
        receipts[effect_id] = {
            "effect_id": effect_id,
            "status": str(row["status"]),
            "target_domain": str(row["target_domain"]),
            "effect_type": str(row["effect_type"]),
            "payload_sha256": str(row["payload_sha256"]),
            "deferred_at": _now(),
            "disposition": "operator_reauthorization_required_after_fence_clear",
        }
    updated = dict(fence.payload)
    reconciliation = dict(updated.get("reconciliation") or {})
    prior = dict(reconciliation.get("sources_effect_quarantine") or {})
    prior.update(receipts)
    reconciliation["sources_effect_quarantine"] = prior
    updated["reconciliation"] = reconciliation
    write_restore_fence(updated, path=fence.path)
    return receipts


def inspect_source_foundation_cohorts(
    *,
    marker_path: str | Path | None = None,
    paths: SourceFoundationPaths | None = None,
) -> dict[str, Any]:
    """Return content-free blockers; an invalid marker is always blocking."""

    fence = read_restore_fence(marker_path)
    if not fence.active:
        return {
            "schema": "wb.source-foundation-restore-status/v1",
            "state": "clear",
            "snapshotId": None,
            "blockers": [],
            "cohorts": {},
        }
    if not fence.valid or fence.payload is None:
        blocker = _finding(
            fence.error or "restore_fence_invalid",
            "The restore marker is malformed and cannot be cleared automatically",
        )
        return {
            "schema": "wb.source-foundation-restore-status/v1",
            "state": "blocked",
            "snapshotId": None,
            "blockers": [blocker],
            "cohorts": {"restore_fence": [blocker]},
        }
    current = paths or SourceFoundationPaths.current()
    cohorts = {
        "identity": _inspect_identity(fence.payload, current),
        "agent_sources": _inspect_agent_sources(current, fence.payload),
        "cowork_conversation_dependencies": _inspect_conversation_dependencies(
            current
        ),
        "journal_capture": _inspect_journal(current),
        "task_notes": _inspect_task_notes(current),
        "truth_stores": _inspect_truth_stores(current, fence.payload),
    }
    blockers = [item for values in cohorts.values() for item in values]
    return {
        "schema": "wb.source-foundation-restore-status/v1",
        "state": "ready_to_clear" if not blockers else "blocked",
        "snapshotId": fence.payload["snapshot_id"],
        "blockers": blockers,
        "cohorts": cohorts,
    }


def archive_cleared_restore_fence(
    *,
    marker_path: str | Path | None = None,
    expected_snapshot_id: str,
) -> Path:
    """Atomically retire an exactly inspected marker while keeping an audit copy."""

    with restore_fence_lock(marker_path):
        fence = read_restore_fence(marker_path)
        if (
            not fence.active
            or not fence.valid
            or fence.payload is None
            or fence.payload.get("snapshot_id") != expected_snapshot_id
        ):
            raise ValueError("source_foundation_restore_fence_changed")
        if fence.path.name != RESTORE_FENCE_FILENAME:
            raise ValueError("refusing_to_archive_unexpected_restore_fence")
        archive_dir = fence.path.parent / "source_foundation_restore_reconciled"
        archive_dir.mkdir(parents=True, exist_ok=True)
        safe_snapshot = "".join(
            character if character.isalnum() or character in "-_." else "_"
            for character in expected_snapshot_id
        )
        destination = archive_dir / f"{safe_snapshot}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        if destination.exists():
            raise ValueError("source_foundation_restore_receipt_exists")
        os.replace(fence.path, destination)
    return destination


__all__ = [
    "SourceFoundationPaths",
    "archive_cleared_restore_fence",
    "inspect_source_foundation_cohorts",
    "quarantine_and_reconstitute_missing_cohorts",
    "quarantine_imported_source_effects",
    "reconcile_portable_truth_stores",
    "reconstitute_sanitized_identity",
    "reconstitute_sources_from_archive",
    "record_identity_trust",
    "validate_sanitized_identity_enrollment",
]
