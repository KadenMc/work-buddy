"""High-consent operator for Source Foundation restore reconciliation."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from work_buddy.backups.source_foundation_reconciliation import (
    SourceFoundationPaths,
    archive_cleared_restore_fence,
    inspect_source_foundation_cohorts,
    quarantine_and_reconstitute_missing_cohorts,
    quarantine_imported_source_effects,
    reconcile_portable_truth_stores,
    record_identity_trust,
    reconstitute_sanitized_identity,
    reconstitute_sources_from_archive,
    validate_sanitized_identity_enrollment,
)
from work_buddy.backups.source_foundation_restore import (
    authorized_restore_reconciliation,
    read_restore_fence,
    restore_fence_lock,
)
from work_buddy.consent import ConsentPrompt, requires_consent
from work_buddy.mcp_server.op_registry import register_op
from work_buddy.sources.models import canonical_sha256


def _prompt(
    snapshot_id: str,
    identity_enrollment_path: str | None,
    disclosure_outcomes: Mapping[str, str] | None,
    recovery_scope: Mapping[str, Any] | None = None,
) -> ConsentPrompt:
    context = {
        "snapshot_id": snapshot_id,
        "identity_enrollment_path": (
            str(Path(identity_enrollment_path).expanduser().resolve())
            if identity_enrollment_path
            else None
        ),
        "disclosure_outcomes": dict(sorted((disclosure_outcomes or {}).items())),
        "recovery_scope": dict(recovery_scope or {}),
    }
    return ConsentPrompt(
        body=(
            "Trust the displayed sanitized enrollment, record only the displayed "
            "transport outcomes without replay, and clear the restore fence only "
            "if every Source Foundation cohort validates."
        ),
        fingerprint=canonical_sha256(
            {
                "schema": "wb.source-foundation-restore-authorization/v1",
                **context,
            }
        ),
        context=context,
    )


@requires_consent(
    "source_foundation.restore_reconcile",
    "Reconcile Source Foundation authority after a machine restore.",
    risk="high",
    consent_weight="high",
    grant_policy="per_invocation",
    request_factory=_prompt,
)
def _authorize(
    snapshot_id: str,
    identity_enrollment_path: str | None,
    disclosure_outcomes: Mapping[str, str] | None,
    recovery_scope: Mapping[str, Any] | None = None,
) -> None:
    return None


def _default_enrollment_path(marker_payload: Mapping[str, Any], marker: Path) -> Path:
    value = marker_payload.get("identity_enrollment")
    member = value.get("member") if isinstance(value, Mapping) else None
    if not isinstance(member, str) or not member or Path(member).name != member:
        raise ValueError("identity_enrollment_path_is_required")
    return marker.parent / member


def _frozen_inventory_sha256(payload: Mapping[str, Any]) -> str:
    """Bind consent to the marker's immutable recovery inventory."""

    return canonical_sha256(
        {
            "schema": "wb.source-foundation-frozen-inventory/v1",
            "snapshot_id": payload.get("snapshot_id"),
            "identity_enrollment": payload.get("identity_enrollment"),
            "portable_truth_root": payload.get("portable_truth_root"),
            "truth_stores": payload.get("truth_stores"),
        }
    )


def _reconcile_disclosures(
    paths: SourceFoundationPaths,
    outcomes: Mapping[str, str],
) -> list[dict[str, str]]:
    from work_buddy.agent_execution.disclosure import (
        DisclosureGateway,
        DisclosureManifestStore,
        DisclosureState,
    )
    from work_buddy.sources.disclosure import SourcesDisclosureService
    from work_buddy.sources.store import SourceStore

    identity = paths.local_identity_db
    import sqlite3

    conn = sqlite3.connect(f"file:{identity.resolve()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM local_identity_meta WHERE key='tenant_scope_id'"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError("local_identity_enrollment_incomplete")
    sources_store = SourceStore.open(paths.sources_root)
    manifest = DisclosureManifestStore(paths.agent_execution_db)
    gateway = DisclosureGateway(
        manifest,
        SourcesDisclosureService(
            sources_store,
            tenant_scope_id=str(row[0]),
        ),
    )
    reconciled: list[dict[str, str]] = []
    prepared: list[tuple[Any, DisclosureState]] = []
    for entry_id, outcome in sorted(outcomes.items()):
        if outcome not in {"sent", "not_sent"}:
            raise ValueError("disclosure outcome must be sent or not_sent")
        before = manifest.get_entry(entry_id)
        proven = DisclosureState(outcome)
        if before.state is not DisclosureState.POSSIBLY_SENT and (
            before.state is not proven or not before.send_attempted
        ):
            raise ValueError(
                "operator outcomes must resolve possibly_sent disclosures or "
                "idempotently repeat their proven result"
            )
        prepared.append((before, proven))

    # The stores above are opened through the ordinary fenced read-only path.
    # Only the exact, prevalidated accounting transitions receive the narrow
    # reconciliation bypass; store initialization, migration, blob recovery,
    # and any transport path remain outside it.
    with authorized_restore_reconciliation():
        # Validate the complete caller-supplied outcome set before applying any
        # of it. Cross-database acknowledgement remains restart-safe and
        # idempotent.
        for before, proven in prepared:
            if before.state is DisclosureState.POSSIBLY_SENT:
                after = gateway.reconcile(before.id, proven_outcome=proven)
            elif before.source_acknowledgement.value == "pending":
                after = gateway.reconcile_acknowledgement(before.id)
            else:
                after = before
            reconciled.append({"entryId": after.id, "outcome": after.state.value})

        # A recorded sent outcome with a pending Sources acknowledgement is safe
        # to resume: this retries accounting only and never invokes transport.
        for item in manifest.list_recovery():
            if item.reason != "source_ack_pending":
                continue
            after = gateway.reconcile_acknowledgement(item.entry.id)
            reconciled.append({"entryId": after.id, "outcome": after.state.value})
    return reconciled


def source_foundation_restore_operator(
    action: str,
    snapshot_id: str | None = None,
    identity_enrollment_path: str | None = None,
    disclosure_outcomes: Mapping[str, str] | None = None,
    reconstitute_missing_identity: bool = False,
    sources_archive_path: str | None = None,
    truth_recovery_targets: Mapping[str, str] | None = None,
    quarantine_truth_store_ids: list[str] | tuple[str, ...] | None = None,
    quarantine_missing_cohorts: list[str] | tuple[str, ...] | None = None,
    defer_source_effect_ids: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Inspect or explicitly reconcile one durable restore fence."""

    if action == "status":
        return inspect_source_foundation_cohorts()
    if action != "reconcile":
        raise ValueError("action must be status or reconcile")
    fence = read_restore_fence()
    if not fence.active or not fence.valid or fence.payload is None:
        raise ValueError(fence.error or "source_foundation_restore_fence_unavailable")
    actual_snapshot_id = str(fence.payload["snapshot_id"])
    if snapshot_id != actual_snapshot_id:
        raise ValueError("snapshot_id does not match the active restore fence")
    outcomes = dict(disclosure_outcomes or {})
    enrollment = (
        Path(identity_enrollment_path).expanduser().resolve()
        if identity_enrollment_path
        else _default_enrollment_path(fence.payload, fence.path)
    )
    paths = SourceFoundationPaths.current()
    truth_targets = {
        str(store_id): str(Path(target).expanduser().resolve())
        for store_id, target in (truth_recovery_targets or {}).items()
    }
    quarantine_ids = tuple(sorted(set(quarantine_truth_store_ids or ())))
    missing_cohorts = tuple(sorted(set(quarantine_missing_cohorts or ())))
    deferred_source_effects = tuple(sorted(set(defer_source_effect_ids or ())))
    enrollment_bytes = enrollment.read_bytes()
    sources_archive = (
        Path(sources_archive_path).expanduser().resolve()
        if sources_archive_path
        else None
    )
    recovery_scope = {
        "frozen_inventory_sha256": _frozen_inventory_sha256(fence.payload),
        "identity_enrollment_sha256": hashlib.sha256(enrollment_bytes).hexdigest(),
        "reconstitute_missing_identity": bool(reconstitute_missing_identity),
        "sources_archive_path": (
            str(sources_archive) if sources_archive else None
        ),
        "sources_archive_sha256": (
            hashlib.sha256(sources_archive.read_bytes()).hexdigest()
            if sources_archive
            else None
        ),
        "truth_recovery_targets": dict(sorted(truth_targets.items())),
        "quarantine_truth_store_ids": list(quarantine_ids),
        "quarantine_missing_cohorts": list(missing_cohorts),
        "defer_source_effect_ids": list(deferred_source_effects),
    }
    _authorize(
        actual_snapshot_id,
        str(enrollment),
        outcomes,
        recovery_scope,
    )
    with restore_fence_lock(fence.path):
        current = read_restore_fence(fence.path)
        if (
            not current.active
            or not current.valid
            or current.payload is None
            or current.payload.get("snapshot_id") != actual_snapshot_id
        ):
            raise ValueError("source_foundation_restore_fence_changed")
        if _frozen_inventory_sha256(current.payload) != recovery_scope[
            "frozen_inventory_sha256"
        ]:
            raise ValueError("source_foundation_restore_inventory_changed")
        identity_record = current.payload.get("identity_enrollment")
        expected_identity_digest = (
            identity_record.get("sha256")
            if isinstance(identity_record, Mapping)
            else None
        )
        # Validate both explicit reconciliation inputs before mutating either
        # authority. Recovery remains restart-safe if the process stops between
        # the independently durable accounting and marker receipts.
        expected_identity_sha = (
            str(expected_identity_digest) if expected_identity_digest else None
        )
        reconstituted_identity = None
        reconstituted_sources = None
        truth_recovery: dict[str, Any] = {"recovered": {}, "quarantined": {}}
        missing_cohort_receipts: dict[str, Any] = {}
        source_effect_quarantine: dict[str, Any] = {}
        with authorized_restore_reconciliation():
            if not paths.local_identity_db.is_file():
                if not reconstitute_missing_identity:
                    raise ValueError("local_identity_reconstitution_requires_consent")
                reconstituted_identity = reconstitute_sanitized_identity(
                    enrollment,
                    local_identity_db=paths.local_identity_db,
                    expected_sha256=expected_identity_sha,
                )
            validate_sanitized_identity_enrollment(
                enrollment,
                local_identity_db=paths.local_identity_db,
                expected_sha256=expected_identity_sha,
            )
            if not (paths.sources_root / "store.db").is_file():
                if not sources_archive_path:
                    raise ValueError("sources_reconstitution_archive_required")
                from work_buddy.sources.models import ActorRef

                with sqlite3.connect(paths.local_identity_db) as conn:
                    identity_values = dict(
                        conn.execute(
                            "SELECT key,value FROM local_identity_meta WHERE key IN "
                            "('issuer_authority_id','tenant_scope_id','local_actor_id')"
                        )
                    )
                reconstituted_sources = reconstitute_sources_from_archive(
                    sources_archive_path,
                    paths=paths,
                    principal=ActorRef(
                        schema="wb.actor-ref/v1",
                        issuer_authority_id=identity_values["issuer_authority_id"],
                        subject=identity_values["local_actor_id"],
                        kind="human",
                        tenant_scope_id=identity_values["tenant_scope_id"],
                    ),
                    authorization_fingerprint=canonical_sha256(
                        {
                            "schema": "wb.source-foundation-restore-sources/v1",
                            "snapshot_id": actual_snapshot_id,
                            "recovery_scope": recovery_scope,
                        }
                    ),
                    expected_sha256=str(recovery_scope["sources_archive_sha256"]),
                )
            truth_recovery = reconcile_portable_truth_stores(
                marker_path=str(fence.path),
                paths=paths,
                recovery_targets=truth_targets,
                quarantine_store_ids=quarantine_ids,
            )
            missing_cohort_receipts = quarantine_and_reconstitute_missing_cohorts(
                missing_cohorts,
                marker_path=str(fence.path),
                paths=paths,
            )
            source_effect_quarantine = quarantine_imported_source_effects(
                deferred_source_effects,
                marker_path=str(fence.path),
                paths=paths,
            )
        reconciled = _reconcile_disclosures(paths, outcomes)
        identity_trust = record_identity_trust(
            enrollment,
            marker_path=fence.path,
            paths=paths,
        )
        status = inspect_source_foundation_cohorts(marker_path=fence.path, paths=paths)
        if status["state"] != "ready_to_clear":
            return {
                **status,
                "identityTrust": identity_trust,
                "reconciledDisclosures": reconciled,
                "reconstitutedIdentity": reconstituted_identity,
                "reconstitutedSources": reconstituted_sources,
                "truthRecovery": truth_recovery,
                "missingCohortQuarantine": missing_cohort_receipts,
                "sourceEffectQuarantine": source_effect_quarantine,
                "cleared": False,
            }
        receipt = archive_cleared_restore_fence(
            marker_path=fence.path,
            expected_snapshot_id=actual_snapshot_id,
        )
    return {
        **status,
        "state": "clear",
        "identityTrust": identity_trust,
        "reconciledDisclosures": reconciled,
        "reconstitutedIdentity": reconstituted_identity,
        "reconstitutedSources": reconstituted_sources,
        "truthRecovery": truth_recovery,
        "missingCohortQuarantine": missing_cohort_receipts,
        "sourceEffectQuarantine": source_effect_quarantine,
        "cleared": True,
        "receiptPath": str(receipt),
    }


register_op(
    "op.wb.source_foundation_restore_operator",
    source_foundation_restore_operator,
    replace=True,
)


__all__ = ["source_foundation_restore_operator"]
