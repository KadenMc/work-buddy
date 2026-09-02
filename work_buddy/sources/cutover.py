"""Stable, least-privilege Sources authorization for legacy cutovers.

The cutover operator constructs this value from an explicitly authorized,
frozen identity.  Every domain receives its own import purpose plus ``export``;
the service principal and authorization fingerprint remain identical so one
post-staging export can cover the complete import cohort.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
from typing import Sequence

from work_buddy.sources.errors import InvalidSourceRequest
from work_buddy.sources.export import (
    ExportAuthorization,
    ExportResult,
    ImportAuthorization,
    export_sources,
)
from work_buddy.sources.ingress import TrustedIngressContext
from work_buddy.sources.models import ActorRef, SourceRef, validate_sha256
from work_buddy.sources.store import SourceStore


CUTOVER_IMPORT_PURPOSES = {
    "journal": "journal.history_import",
    "projects": "projects.history_import",
    "personal_knowledge": "personal_knowledge.history_import",
    "contracts": "contracts.history_import",
}


class CutoverSourceDependencyError(RuntimeError):
    """A cutover dependency database cannot be inspected safely."""


@dataclass(frozen=True, slots=True)
class CutoverSourceDependencyParity:
    """Content-free dependency parity for one four-domain cutover cohort."""

    journal_count: int
    journal_gaps: int
    projects_count: int
    projects_gaps: int
    personal_knowledge_count: int
    personal_knowledge_gaps: int
    contracts_count: int
    contracts_gaps: int

    @property
    def total_count(self) -> int:
        return (
            self.journal_count
            + self.projects_count
            + self.personal_knowledge_count
            + self.contracts_count
        )

    @property
    def total_gaps(self) -> int:
        return (
            self.journal_gaps
            + self.projects_gaps
            + self.personal_knowledge_gaps
            + self.contracts_gaps
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "wb.cutover-source-dependency-parity/v1",
            "totalCount": self.total_count,
            "totalGaps": self.total_gaps,
            "domains": {
                "journal": {
                    "count": self.journal_count,
                    "gaps": self.journal_gaps,
                },
                "projects": {
                    "count": self.projects_count,
                    "gaps": self.projects_gaps,
                },
                "personalKnowledge": {
                    "count": self.personal_knowledge_count,
                    "gaps": self.personal_knowledge_gaps,
                },
                "contracts": {
                    "count": self.contracts_count,
                    "gaps": self.contracts_gaps,
                },
            },
        }


_DEPENDENCY_QUERIES = {
    "journal": (
        "SELECT f.relative_path AS item_key,f.raw_sha256 AS expected_sha256,"
        "f.byte_length AS expected_byte_length,f.source_ref,f.representation_id,"
        "f.source_usage_id,f.source_usage_state FROM journal_import_files f "
        "WHERE f.cohort_id=? ORDER BY f.relative_path",
        "SELECT expected_file_count FROM journal_import_cohorts WHERE cohort_id=?",
    ),
    "projects": (
        "SELECT i.relative_path AS item_key,i.source_sha256 AS expected_sha256,"
        "i.byte_length AS expected_byte_length,d.source_ref,d.representation_id,"
        "d.source_usage_id,d.source_usage_state "
        "FROM project_import_source_dependencies d JOIN project_import_items i "
        "USING(cohort_id,relative_path) WHERE d.cohort_id=? ORDER BY i.relative_path",
        "SELECT file_count FROM project_import_cohorts WHERE cohort_id=?",
    ),
    "personal_knowledge": (
        "SELECT i.relative_path AS item_key,i.source_sha256 AS expected_sha256,"
        "i.byte_length AS expected_byte_length,d.source_ref,d.representation_id,"
        "d.source_usage_id,d.source_usage_state "
        "FROM personal_import_source_dependencies d JOIN personal_import_items i "
        "USING(cohort_id,relative_path) WHERE d.cohort_id=? ORDER BY i.relative_path",
        "SELECT file_count FROM personal_import_cohorts WHERE cohort_id=?",
    ),
    "contracts": (
        "SELECT i.source_key AS item_key,i.source_sha256 AS expected_sha256,"
        "i.byte_length AS expected_byte_length,d.source_ref,d.representation_id,"
        "d.source_usage_id,d.source_usage_state "
        "FROM contract_import_source_dependencies d JOIN contract_import_inventory i "
        "USING(cohort_id,source_key) WHERE d.cohort_id=? ORDER BY i.source_key",
        "SELECT item_count FROM contract_import_cohorts WHERE cohort_id=?",
    ),
}


def _read_only_connection(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise CutoverSourceDependencyError("a cutover dependency database is unavailable")
    try:
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error as exc:
        raise CutoverSourceDependencyError(
            "a cutover dependency database cannot be opened read-only"
        ) from exc


def _dependency_rows(
    path: str | Path,
    *,
    domain: str,
    cohort_id: str,
) -> tuple[list[sqlite3.Row], int]:
    query, expected_query = _DEPENDENCY_QUERIES[domain]
    try:
        with _read_only_connection(path) as connection:
            expected_row = connection.execute(expected_query, (cohort_id,)).fetchone()
            if expected_row is None:
                raise CutoverSourceDependencyError(
                    "a cutover dependency cohort is unavailable"
                )
            rows = connection.execute(query, (cohort_id,)).fetchall()
    except sqlite3.Error as exc:
        raise CutoverSourceDependencyError(
            "a cutover dependency database has an incompatible schema"
        ) from exc
    return rows, int(expected_row[0])


def _dependency_parity(
    store: SourceStore,
    *,
    domain: str,
    rows: list[sqlite3.Row],
    expected_count: int,
) -> tuple[int, int]:
    gaps = abs(expected_count - len(rows))
    with store.connect() as connection:
        for row in rows:
            valid = True
            try:
                ref = SourceRef.parse(str(row["source_ref"]))
                representation_id = str(row["representation_id"])
                usage_id = str(row["source_usage_id"])
                if (
                    row["source_usage_state"] != "acknowledged"
                    or not representation_id
                    or not usage_id
                ):
                    valid = False
                usage = connection.execute(
                    "SELECT authority_id,source_item_id,representation_id,purpose,status "
                    "FROM source_usage_intents WHERE usage_id=?",
                    (usage_id,),
                ).fetchone()
                if (
                    usage is None
                    or str(usage["authority_id"]) != ref.authority_id
                    or str(usage["source_item_id"]) != ref.item_id
                    or str(usage["representation_id"]) != representation_id
                    or str(usage["purpose"]) != CUTOVER_IMPORT_PURPOSES[domain]
                    or str(usage["status"]) != "acknowledged"
                ):
                    valid = False
                representation = store._representation_row(
                    connection,
                    ref,
                    representation_id,
                )
                content = store._read_representation_row(representation)
                if (
                    len(content) != int(row["expected_byte_length"])
                    or hashlib.sha256(content).hexdigest()
                    != str(row["expected_sha256"])
                ):
                    valid = False
            except Exception:
                valid = False
            if not valid:
                gaps += 1
    return len(rows), gaps


def verify_cutover_source_dependencies(
    store: SourceStore,
    *,
    journal_db: str | Path,
    journal_cohort_id: str,
    projects_db: str | Path,
    projects_cohort_id: str,
    personal_knowledge_db: str | Path,
    personal_knowledge_cohort_id: str,
    contracts_db: str | Path,
    contracts_cohort_id: str,
) -> CutoverSourceDependencyParity:
    """Verify restored exact bytes and acknowledged usages for all four domains.

    The returned value contains counts only.  Paths and item identifiers never
    leave this inspection boundary, so it is safe to include in a cutover
    receipt or operator report.
    """

    inputs = {
        "journal": (journal_db, journal_cohort_id),
        "projects": (projects_db, projects_cohort_id),
        "personal_knowledge": (
            personal_knowledge_db,
            personal_knowledge_cohort_id,
        ),
        "contracts": (contracts_db, contracts_cohort_id),
    }
    results: dict[str, tuple[int, int]] = {}
    for domain, (path, cohort_id) in inputs.items():
        rows, expected_count = _dependency_rows(
            path,
            domain=domain,
            cohort_id=cohort_id,
        )
        results[domain] = _dependency_parity(
            store,
            domain=domain,
            rows=rows,
            expected_count=expected_count,
        )
    return CutoverSourceDependencyParity(
        journal_count=results["journal"][0],
        journal_gaps=results["journal"][1],
        projects_count=results["projects"][0],
        projects_gaps=results["projects"][1],
        personal_knowledge_count=results["personal_knowledge"][0],
        personal_knowledge_gaps=results["personal_knowledge"][1],
        contracts_count=results["contracts"][0],
        contracts_gaps=results["contracts"][1],
    )


@dataclass(frozen=True, slots=True)
class CutoverSourceAuthorization:
    """Frozen trusted-ingress identity shared by all cutover domains."""

    issuer: ActorRef
    inputter: ActorRef
    principal: ActorRef
    tenant_scope_id: str
    authorization_fingerprint: str
    issuer_version: str = "work-buddy-cutover/v1"
    namespace: str = "legacy-domain-cutover"
    sensitivity_class: str = "private"
    retention_class: str = "durable"
    inputter_assurance: str = "historical_inputter_only"

    def __post_init__(self) -> None:
        validate_sha256(self.authorization_fingerprint)
        actors = (self.issuer, self.inputter, self.principal)
        if any(actor.tenant_scope_id != self.tenant_scope_id for actor in actors):
            raise InvalidSourceRequest()
        if self.issuer.kind != "service" or self.principal.kind != "service":
            raise InvalidSourceRequest()
        for value in (
            self.tenant_scope_id,
            self.issuer_version,
            self.namespace,
            self.sensitivity_class,
            self.retention_class,
            self.inputter_assurance,
        ):
            if not isinstance(value, str) or not value or len(value) > 256:
                raise InvalidSourceRequest()

    def ingress_context(self, domain: str) -> TrustedIngressContext:
        """Build the deterministic context for one supported import domain."""

        try:
            import_purpose = CUTOVER_IMPORT_PURPOSES[domain]
        except KeyError as exc:
            raise InvalidSourceRequest() from exc
        return TrustedIngressContext(
            issuer=self.issuer,
            issuer_version=self.issuer_version,
            inputter=self.inputter,
            service_principal=self.principal,
            tenant_scope_id=self.tenant_scope_id,
            surface=f"{domain}-history-import",
            namespace=self.namespace,
            sensitivity_class=self.sensitivity_class,
            retention_class=self.retention_class,
            inputter_assurance=self.inputter_assurance,
            authorization_fingerprint=self.authorization_fingerprint,
            permitted_purposes=(import_purpose, "export"),
        )

    def export_authorization(self) -> ExportAuthorization:
        """Authorize an exact-content export under the frozen cutover grant."""

        return ExportAuthorization(
            principal=self.principal,
            authorization_fingerprint=self.authorization_fingerprint,
            include_content=True,
        )

    def restore_authorization(self) -> ImportAuthorization:
        """Authorize an identity-preserving operational restore rehearsal."""

        return ImportAuthorization(
            principal=self.principal,
            authorization_fingerprint=self.authorization_fingerprint,
            allow_foreign_authorities=True,
            collision_policy="reject",
            restore_operational_state=True,
        )

    def merge_authorization(self) -> ImportAuthorization:
        """Authorize an exact identity-preserving delta merge into a restored base.

        A full sensitive checkpoint restore must restore operational state into
        an empty Sources authority.  The subsequent cutover delta is different:
        it is merged into that restored base, then each domain deterministically
        replays staging to recreate and acknowledge its operational usages.
        """

        return ImportAuthorization(
            principal=self.principal,
            authorization_fingerprint=self.authorization_fingerprint,
            allow_foreign_authorities=True,
            collision_policy="reject",
            restore_operational_state=False,
            merge_operational_state=True,
        )

    def export_after_staging(
        self,
        store: SourceStore,
        destination: str | Path,
        *,
        source_refs: Sequence[SourceRef] | None = None,
        idempotency_key: str,
    ) -> ExportResult:
        """Export a frozen staged scope using the same ingress authorization.

        ``idempotency_key`` is mandatory because cutover retries must reconcile
        the original export instead of issuing another offline copy.
        """

        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise InvalidSourceRequest()
        return export_sources(
            store,
            destination,
            authorization=self.export_authorization(),
            source_refs=source_refs,
            idempotency_key=idempotency_key,
        )


__all__ = [
    "CUTOVER_IMPORT_PURPOSES",
    "CutoverSourceAuthorization",
    "CutoverSourceDependencyError",
    "CutoverSourceDependencyParity",
    "verify_cutover_source_dependencies",
]
