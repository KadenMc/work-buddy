"""Rehearse Projects, Personal Knowledge, and Contracts cutover in temp stores.

The command reads configured legacy roots, writes only beneath an operating-
system temporary directory, and prints a content-free JSON receipt containing
counts and inventory digests.  It never changes configuration or live
authority state.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from work_buddy.config import load_config
from work_buddy.contracts_domain.importer import ContractImporter
from work_buddy.contracts_domain.store import ContractStore
from work_buddy.cutover_maintenance import authorize_isolated_rehearsal_root
from work_buddy.knowledge.personal.importer import PersonalKnowledgeImportCoordinator
from work_buddy.knowledge.personal.legacy import configured_legacy_root
from work_buddy.knowledge.personal.store import PersonalKnowledgeStore
from work_buddy.projects import store as project_store
from work_buddy.projects.authority import ProjectImportCoordinator
from work_buddy.sources import (
    ActorRef,
    CutoverSourceAuthorization,
    SourceStore,
    import_sources,
)


def _authorization() -> CutoverSourceAuthorization:
    tenant = "tenant-cutover-rehearsal"
    authority = "rehearsal-authority"
    return CutoverSourceAuthorization(
        issuer=ActorRef(authority, "cutover-rehearsal", "service", tenant),
        inputter=ActorRef(authority, "legacy-owner", "human", tenant),
        principal=ActorRef(authority, "cutover-operator", "service", tenant),
        tenant_scope_id=tenant,
        authorization_fingerprint="1" * 64,
        issuer_version="rehearsal/v1",
        namespace="cutover-rehearsal",
    )


def rehearse() -> dict[str, object]:
    config = load_config()
    vault = Path(config["vault_root"]).expanduser().resolve()
    roots = {
        "projects": (
            vault
            / config.get("projects", {}).get(
                "markdown_dir", "work-buddy/projects"
            )
        ).resolve(),
        "contracts": (
            vault
            / config.get("contracts", {}).get(
                "vault_path", "work-buddy/contracts"
            )
        ).resolve(),
        "personal": configured_legacy_root(),
    }
    if any(path is None or not path.is_dir() for path in roots.values()):
        raise RuntimeError("a configured legacy import root is unavailable")

    with tempfile.TemporaryDirectory(prefix="wb-cutover-rehearsal-") as temporary:
        root = Path(temporary)
        sources = SourceStore.create(
            root / "sources", authority_id="rehearsal-source-authority"
        )
        authorization = _authorization()

        original_project_db_path = project_store._db_path
        project_store._db_path = lambda: root / "projects.db"
        try:
            projects = ProjectImportCoordinator(sources)
            project_prepared = projects.prepare(
                cohort_id="projects-configured-rehearsal-v1",
                source_root=roots["projects"],
                ingress_context=authorization.ingress_context("projects"),
            )

            personal_store = PersonalKnowledgeStore(root / "personal.db")
            personal = PersonalKnowledgeImportCoordinator(personal_store, sources)
            personal_prepared = personal.prepare(
                cohort_id="personal-configured-rehearsal-v1",
                source_root=roots["personal"],
                ingress_context=authorization.ingress_context("personal_knowledge"),
            )

            contract_store = ContractStore.create(root / "contracts.db")
            contracts = ContractImporter(contract_store, sources)
            contract_staged = contracts.stage(
                roots["contracts"],
                actor="operator:rehearsal",
                intent_id="contracts-configured-rehearsal-stage-v1",
                ingress_context=authorization.ingress_context("contracts"),
            )
            rehearsal_authorization = authorize_isolated_rehearsal_root(
                root,
                authority_paths={
                    "projects": project_store._db_path(),
                    "personal_knowledge": personal_store.db_path,
                    "contracts": contract_store.path,
                },
            )

            source_export = authorization.export_after_staging(
                sources,
                root / "authorized-cutover-sources.jsonl",
                idempotency_key="configured-cutover-post-staging-export-v1",
            )
            restored_sources = SourceStore.create(
                root / "restored-sources",
                authority_id=sources.authority_id,
            )
            restored = import_sources(
                restored_sources,
                source_export.path,
                authorization=authorization.restore_authorization(),
            )
            if (
                restored.quarantined_count != 0
                or restored.remapped_count != 0
                or restored.item_count != source_export.item_count
            ):
                raise RuntimeError("authorized Sources restore parity mismatch")

            projects.verify(project_prepared["cohort_id"])
            project_sealed = projects.seal(
                project_prepared["cohort_id"],
                allow_unfenced_rehearsal=True,
                rehearsal_authorization=rehearsal_authorization,
            )
            if projects.seal(
                project_prepared["cohort_id"],
                allow_unfenced_rehearsal=True,
                rehearsal_authorization=rehearsal_authorization,
            )["state"] != "sealed":
                raise RuntimeError("Project seal replay failed")

            personal.verify(personal_prepared["cohort_id"])
            personal_sealed = personal.seal(
                personal_prepared["cohort_id"],
                allow_unfenced_rehearsal=True,
                rehearsal_authorization=rehearsal_authorization,
            )
            if personal.seal(
                personal_prepared["cohort_id"],
                allow_unfenced_rehearsal=True,
                rehearsal_authorization=rehearsal_authorization,
            )["state"] != "sealed":
                raise RuntimeError("personal seal replay failed")

            contract_verified = contracts.verify(contract_staged["cohort_id"])
            contract_sealed = contracts.seal(
                contract_staged["cohort_id"],
                expected_inventory_sha256=contract_staged["inventory_sha256"],
                actor="operator:rehearsal",
                intent_id="contracts-configured-rehearsal-seal-v1",
                allow_unfenced_rehearsal=True,
                rehearsal_authorization=rehearsal_authorization,
            )
            if contracts.seal(
                contract_staged["cohort_id"],
                expected_inventory_sha256=contract_staged["inventory_sha256"],
                actor="operator:rehearsal",
                intent_id="contracts-configured-rehearsal-seal-v1",
                allow_unfenced_rehearsal=True,
                rehearsal_authorization=rehearsal_authorization,
            ) != contract_sealed:
                raise RuntimeError("Contract seal replay failed")
        finally:
            project_store._db_path = original_project_db_path

        connection = sources.connect()
        try:
            source_items = int(
                connection.execute("SELECT COUNT(*) FROM source_items").fetchone()[0]
            )
            acknowledged = int(
                connection.execute(
                    "SELECT COUNT(*) FROM source_usage_intents "
                    "WHERE status='acknowledged' AND consumer_domain!='export'"
                ).fetchone()[0]
            )
            by_domain = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT consumer_domain,COUNT(*) FROM source_usage_intents "
                    "WHERE status='acknowledged' AND consumer_domain!='export' "
                    "GROUP BY consumer_domain "
                    "ORDER BY consumer_domain"
                )
            }
        finally:
            connection.close()
        expected = int(project_prepared["file_count"]) + int(
            personal_prepared["file_count"]
        ) + int(contract_staged["item_count"])
        expected_by_domain = {
            "contracts": int(contract_staged["item_count"]),
            "personal_knowledge": int(personal_prepared["file_count"]),
            "projects": int(project_prepared["file_count"]),
        }
        if source_items != expected or acknowledged != expected:
            raise RuntimeError("rehearsal Source cardinality mismatch")
        if by_domain != expected_by_domain:
            raise RuntimeError("rehearsal Source domain parity mismatch")
        return {
            "schema": "wb.configured-cutover-rehearsal/v1",
            "projects": {
                "files": project_prepared["file_count"],
                "quarantined": project_prepared["quarantined_count"],
                "imported": project_sealed["importedCount"],
                "inventorySha256": project_prepared["inventory_sha256"],
            },
            "personal": {
                "files": personal_prepared["file_count"],
                "quarantined": personal_prepared["quarantined_count"],
                "imported": personal_sealed["imported_count"],
                "inventorySha256": personal_prepared["inventory_sha256"],
            },
            "contracts": {
                "files": contract_staged["item_count"],
                "accepted": contract_staged["accepted_count"],
                "quarantined": contract_staged["quarantined_count"],
                "ignored": contract_staged["ignored_count"],
                "inventorySha256": contract_staged["inventory_sha256"],
                "verifiedSources": contract_verified[
                    "source_acknowledged_count"
                ],
            },
            "sources": {
                "items": source_items,
                "acknowledgedUsages": acknowledged,
                "byDomain": by_domain,
                "authorizedExportItems": source_export.item_count,
                "authorizedExportSha256": source_export.sha256,
                "restoredItems": restored.item_count,
                "restoreQuarantined": restored.quarantined_count,
                "restoreRemapped": restored.remapped_count,
            },
            "liveWrites": 0,
            "containsProse": False,
        }


if __name__ == "__main__":
    print(json.dumps(rehearse(), sort_keys=True))
