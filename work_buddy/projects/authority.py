"""Explicit authority and deterministic import coordinator for Projects.

The existing Projects SQLite store already owns stable identity and revision
history.  This module supplies the missing cutover boundary: legacy Markdown
may be inventoried and staged, but ordinary reads continue to see the current
store until one verified cohort is sealed in the same SQLite transaction that
makes its rows authoritative.  Opening the database never flips authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from work_buddy.cutover_maintenance import (
    CutoverMaintenanceError,
    IsolatedRehearsalAuthorization,
    mark_postseal_pending,
    pause_cutover_maintenance,
    prior_postseal_release_evidence,
    release_postseal_maintenance,
    resume_preseal_maintenance,
    require_isolated_rehearsal_path,
)
from work_buddy.projects import store as project_store
from work_buddy.installed_authority import (
    confirm_domain_seal,
    mark_domain_released,
    prepare_domain_seal,
)
from work_buddy.projects.note_format import ProjectNoteParseError, parse_project_note
from work_buddy.projects.operation_lock import (
    serialized_project_authority_transition,
)
from work_buddy.sources import SourceStore, TrustedIngressContext
from work_buddy.sources.import_dependency import (
    ExactImportSourceError,
    ExactImportSourceService,
)


IMPORT_PARSER_VERSION = "wb.project-markdown-import/v1"
IMPORT_SOURCE_PURPOSE = "projects.history_import"
IMPORT_SOURCE_USE_KIND = "project_history_import"


class ProjectAuthorityError(RuntimeError):
    """A project authority, inventory, or replay invariant was violated."""


@dataclass(frozen=True, slots=True)
class ProjectImportInventoryItem:
    relative_path: str
    byte_length: int
    mtime_ns: int
    source_sha256: str
    slug: str | None
    payload: Mapping[str, Any] | None
    disposition: str
    reason_code: str | None

    def receipt(self) -> dict[str, Any]:
        return {
            "relativePath": self.relative_path,
            "byteLength": self.byte_length,
            "mtimeNs": self.mtime_ns,
            "sourceSha256": self.source_sha256,
            "slug": self.slug,
            "disposition": self.disposition,
            "reasonCode": self.reason_code,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_json(value: Any) -> str:
    return _sha_bytes(_canonical_json(value).encode("utf-8"))


def _resolved_root(root: str | Path) -> Path:
    value = Path(root).expanduser().resolve()
    if not value.is_dir():
        raise ProjectAuthorityError("Project Markdown source root is unavailable")
    return value


def inventory_project_notes(root: str | Path) -> tuple[ProjectImportInventoryItem, ...]:
    """Parse direct-child project notes as inert UTF-8 input.

    The returned receipt shape deliberately omits descriptions.  Exact prose is
    retained only in the private staging row (and, when supplied by the caller,
    an authorized Source record).
    """

    source_root = _resolved_root(root)
    result: list[ProjectImportInventoryItem] = []
    for path in sorted(source_root.glob("*.md"), key=lambda item: item.name):
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_file():
            raise ProjectAuthorityError("Project import accepts regular files only")
        try:
            resolved.relative_to(source_root)
        except ValueError as exc:
            raise ProjectAuthorityError("Project input escaped its allowlisted root") from exc
        raw = resolved.read_bytes()
        stat = resolved.stat()
        disposition = "staged"
        reason: str | None = None
        slug: str | None = None
        payload: dict[str, Any] | None = None
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            disposition = "quarantined"
            reason = "invalid_utf8"
        else:
            try:
                note = parse_project_note(text)
            except ProjectNoteParseError:
                disposition = "quarantined"
                reason = "invalid_project_note"
            else:
                slug = note.slug
                if note.slug != path.stem:
                    disposition = "quarantined"
                    reason = "slug_filename_mismatch"
                elif note.status not in project_store.VALID_STATUSES:
                    disposition = "quarantined"
                    reason = "invalid_status"
                else:
                    payload = {
                        "slug": note.slug,
                        "name": note.name,
                        "status": note.status,
                        "description": note.description,
                    }
        result.append(
            ProjectImportInventoryItem(
                relative_path=path.name,
                byte_length=len(raw),
                mtime_ns=int(stat.st_mtime_ns),
                source_sha256=_sha_bytes(raw),
                slug=slug,
                payload=payload,
                disposition=disposition,
                reason_code=reason,
            )
        )
    return tuple(result)


def _inventory_sha(items: tuple[ProjectImportInventoryItem, ...]) -> str:
    return _sha_json(
        {
            "parserVersion": IMPORT_PARSER_VERSION,
            "items": [item.receipt() for item in items],
        }
    )


def authority_status() -> dict[str, Any]:
    conn = project_store.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM project_authority_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise ProjectAuthorityError("Project authority state is missing")
        return dict(row)
    finally:
        conn.close()


def sqlite_authority_active() -> bool:
    status = authority_status()
    return status["authority"] == "sqlite" and status["state"] == "active"


def require_markdown_write_allowed() -> None:
    status = authority_status()
    if status["authority"] != "legacy_markdown" or status["state"] != "active":
        raise ProjectAuthorityError(
            "Project Markdown is a frozen legacy surface after SQLite cutover"
        )


class ProjectImportCoordinator:
    """Stage, verify, seal, and replay one legacy Projects cohort."""

    def __init__(
        self,
        source_store: SourceStore | None = None,
        source_committed: Callable[[str, str], None] | None = None,
    ) -> None:
        self.sources = source_store
        self.source_dependencies = (
            ExactImportSourceService(
                source_store,
                purpose=IMPORT_SOURCE_PURPOSE,
                consumer_domain="projects",
                use_kind=IMPORT_SOURCE_USE_KIND,
            )
            if source_store is not None
            else None
        )
        self._source_committed = source_committed or (
            lambda _cohort_id, _relative_path: None
        )

    @serialized_project_authority_transition
    def pause_mutations(
        self,
        *,
        cohort_id: str,
        inventory_sha256: str,
        mutation_id: str,
        actor: str,
    ) -> dict[str, Any]:
        conn = project_store.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authority = conn.execute(
                "SELECT * FROM project_authority_state WHERE singleton=1"
            ).fetchone()
            if authority is None or authority["authority"] != "legacy_markdown":
                raise ProjectAuthorityError("Projects cannot enter preseal maintenance")
            result = pause_cutover_maintenance(
                conn,
                domain="projects",
                cohort_id=cohort_id,
                inventory_sha256=inventory_sha256,
                mutation_id=mutation_id,
                actor_sha256=_sha_json({"actor": actor}),
            )
            conn.execute(
                "UPDATE project_authority_state SET state='write_fenced',updated_at=? "
                "WHERE singleton=1",
                (project_store._now(),),
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @serialized_project_authority_transition
    def resume_preseal_mutations(
        self,
        *,
        cohort_id: str,
        mutation_id: str,
        actor: str,
    ) -> dict[str, Any]:
        conn = project_store.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authority = conn.execute(
                "SELECT * FROM project_authority_state WHERE singleton=1"
            ).fetchone()
            if authority is None or authority["authority"] != "legacy_markdown":
                raise ProjectAuthorityError("sealed Projects maintenance cannot resume")
            result = resume_preseal_maintenance(
                conn,
                domain="projects",
                cohort_id=cohort_id,
                mutation_id=mutation_id,
                actor_sha256=_sha_json({"actor": actor}),
            )
            conn.execute(
                "UPDATE project_authority_state SET state='active',updated_at=? "
                "WHERE singleton=1 AND authority='legacy_markdown'",
                (project_store._now(),),
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @serialized_project_authority_transition
    def release_postseal_mutations(
        self,
        *,
        cohort_id: str,
        mutation_id: str,
        actor: str,
        checkpoint_evidence_path: str | Path | None = None,
        search_evidence_path: str | Path | None = None,
        detachment_evidence_path: str | Path | None = None,
        rehearsal_evidence_sha256s: Mapping[str, str] | None = None,
        allow_unvalidated_rehearsal: bool = False,
        rehearsal_authorization: IsolatedRehearsalAuthorization | None = None,
    ) -> dict[str, Any]:
        conn = project_store.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authority = conn.execute(
                "SELECT * FROM project_authority_state WHERE singleton=1"
            ).fetchone()
            if (
                authority is None
                or authority["authority"] != "sqlite"
                or authority["sealed_cohort_id"] != cohort_id
            ):
                raise ProjectAuthorityError("Projects postseal maintenance is unavailable")
            prior_evidence = prior_postseal_release_evidence(
                conn, mutation_id=mutation_id
            )
            if allow_unvalidated_rehearsal:
                require_isolated_rehearsal_path(
                    project_store._db_path(),
                    domain="projects",
                    authorization=rehearsal_authorization,
                )
                if rehearsal_evidence_sha256s is None:
                    raise ProjectAuthorityError("Projects rehearsal evidence is missing")
                evidence = dict(rehearsal_evidence_sha256s)
                if set(evidence) != {"databaseCheckpoint", "search", "detachment"}:
                    raise CutoverMaintenanceError("postseal evidence is incomplete")
                evidence["authorityHead"] = (
                    prior_evidence["authorityHead"]
                    if prior_evidence is not None
                    else _sha_bytes(project_store._db_path().read_bytes())
                )
            else:
                if (
                    checkpoint_evidence_path is None
                    or search_evidence_path is None
                    or detachment_evidence_path is None
                    or rehearsal_evidence_sha256s is not None
                    or rehearsal_authorization is not None
                ):
                    raise ProjectAuthorityError(
                        "Projects configured postseal evidence is required"
                    )
                from work_buddy.cutover_release import (
                    hash_supplied_postseal_evidence,
                    validate_configured_postseal_evidence,
                )

                if prior_evidence is not None:
                    evidence = hash_supplied_postseal_evidence(
                        checkpoint_evidence_path=checkpoint_evidence_path,
                        search_evidence_path=search_evidence_path,
                        detachment_evidence_path=detachment_evidence_path,
                    )
                    evidence["authorityHead"] = prior_evidence["authorityHead"]
                else:
                    evidence = validate_configured_postseal_evidence(
                        domain="projects",
                        authority_db_path=project_store._db_path(),
                        checkpoint_evidence_path=checkpoint_evidence_path,
                        search_evidence_path=search_evidence_path,
                        detachment_evidence_path=detachment_evidence_path,
                    )
            result = release_postseal_maintenance(
                conn,
                domain="projects",
                cohort_id=cohort_id,
                mutation_id=mutation_id,
                actor_sha256=_sha_json({"actor": actor}),
                evidence_sha256s=evidence,
            )
            conn.execute(
                "UPDATE project_authority_state SET state='active',updated_at=? "
                "WHERE singleton=1 AND authority='sqlite' AND sealed_cohort_id=?",
                (project_store._now(), cohort_id),
            )
            conn.commit()
            mark_domain_released(
                "projects", project_store._db_path(), cohort_id=cohort_id
            )
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def prepare(
        self,
        *,
        cohort_id: str,
        source_root: str | Path,
        source_refs: Mapping[str, str] | None = None,
        ingress_context: TrustedIngressContext | None = None,
    ) -> dict[str, Any]:
        if not cohort_id.strip() or len(cohort_id) > 128:
            raise ValueError("a bounded cohort_id is required")
        if source_refs:
            raise ProjectAuthorityError(
                "caller-supplied Project Source references are not accepted"
            )
        authority = authority_status()
        if authority["authority"] == "sqlite":
            if authority["sealed_cohort_id"] == cohort_id:
                return self.status(cohort_id)
            raise ProjectAuthorityError("Projects already use SQLite authority")
        if ingress_context is None:
            raise ProjectAuthorityError(
                "Project import requires trusted ingress context"
            )
        source_dependencies = self._require_source_dependencies(ingress_context)
        root = _resolved_root(source_root)
        items = inventory_project_notes(root)
        inventory_sha = _inventory_sha(items)
        request_sha = _sha_json(
            {
                "cohortId": cohort_id,
                "inventorySha256": inventory_sha,
                "parserVersion": IMPORT_PARSER_VERSION,
            }
        )
        now = project_store._now()
        conn = project_store.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authority = conn.execute(
                "SELECT authority,state,sealed_cohort_id FROM project_authority_state "
                "WHERE singleton=1"
            ).fetchone()
            if authority is None:
                raise ProjectAuthorityError("Project authority state is missing")
            existing = conn.execute(
                "SELECT request_sha256 FROM project_import_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha:
                    raise ProjectAuthorityError(
                        "cohort_id was already used for a different inventory"
                    )
                conn.rollback()
            else:
                quarantined = sum(item.disposition == "quarantined" for item in items)
                conn.execute(
                "INSERT INTO project_import_cohorts "
                "(cohort_id,request_sha256,inventory_sha256,source_root,state,"
                " file_count,quarantined_count,prepared_at) "
                "VALUES (?,?,?,?, 'prepared',?,?,?)",
                (
                    cohort_id,
                    request_sha,
                    inventory_sha,
                    str(root),
                    len(items),
                    quarantined,
                    now,
                ),
            )
                for item in items:
                    conn.execute(
                    "INSERT INTO project_import_items "
                    "(cohort_id,relative_path,source_sha256,byte_length,mtime_ns,"
                    " slug,payload_json,disposition,reason_code,source_ref) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        cohort_id,
                        item.relative_path,
                        item.source_sha256,
                        item.byte_length,
                        item.mtime_ns,
                        item.slug,
                        _canonical_json(item.payload) if item.payload is not None else None,
                        item.disposition,
                        item.reason_code,
                        None,
                    ),
                    )
                    conn.execute(
                        "INSERT INTO project_import_source_dependencies "
                        "(cohort_id,relative_path,ingress_client_mutation_id,"
                        "source_usage_consumer_id) VALUES (?,?,?,?)",
                        (
                            cohort_id,
                            item.relative_path,
                            self._ingress_mutation_id(cohort_id, item.relative_path),
                            self._source_consumer_id(cohort_id, item.relative_path),
                        ),
                    )
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._stage_sources(cohort_id, root, ingress_context, source_dependencies)
        return self.status(cohort_id)

    def status(self, cohort_id: str) -> dict[str, Any]:
        conn = project_store.get_connection()
        try:
            row = conn.execute(
                "SELECT cohort_id,request_sha256,inventory_sha256,state,file_count,"
                "quarantined_count,prepared_at,verified_at,sealed_at,aborted_at "
                "FROM project_import_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if row is None:
                raise ProjectAuthorityError("Unknown project import cohort")
            result = dict(row)
            source_counts = conn.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN source_usage_state='acknowledged' THEN 1 ELSE 0 END) "
                "AS acknowledged FROM project_import_source_dependencies "
                "WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            result["source_count"] = int(source_counts["total"] or 0)
            result["source_acknowledged_count"] = int(
                source_counts["acknowledged"] or 0
            )
            return result
        finally:
            conn.close()

    def verify(self, cohort_id: str, *, allow_quarantined: bool = False) -> dict[str, Any]:
        conn = project_store.get_connection()
        try:
            cohort = conn.execute(
                "SELECT * FROM project_import_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if cohort is None:
                raise ProjectAuthorityError("Unknown project import cohort")
            if cohort["state"] == "sealed":
                return self.status(cohort_id)
            if cohort["state"] == "aborted":
                raise ProjectAuthorityError("An aborted cohort cannot be verified")
            dependencies = conn.execute(
                "SELECT d.*,i.source_sha256,i.byte_length "
                "FROM project_import_source_dependencies d "
                "JOIN project_import_items i USING(cohort_id,relative_path) "
                "WHERE d.cohort_id=? ORDER BY d.relative_path",
                (cohort_id,),
            ).fetchall()
            if len(dependencies) != int(cohort["file_count"]) or any(
                row["source_usage_state"] != "acknowledged" for row in dependencies
            ):
                raise ProjectAuthorityError(
                    "Project cohort has incomplete Source dependencies"
                )
            source_dependencies = self._require_source_dependencies(None)
            for dependency in dependencies:
                try:
                    source_dependencies.verify_exact(
                        source_ref=str(dependency["source_ref"]),
                        representation_id=str(dependency["representation_id"]),
                        expected_sha256=str(dependency["source_sha256"]),
                        expected_byte_length=int(dependency["byte_length"]),
                    )
                except ExactImportSourceError as exc:
                    raise ProjectAuthorityError(str(exc)) from exc
            actual = inventory_project_notes(cohort["source_root"])
            if _inventory_sha(actual) != cohort["inventory_sha256"]:
                raise ProjectAuthorityError("The frozen Project corpus changed")
            if cohort["quarantined_count"] and not allow_quarantined:
                raise ProjectAuthorityError("Project cohort contains quarantined notes")
            conn.execute(
                "UPDATE project_import_cohorts SET state='verified',verified_at=? "
                "WHERE cohort_id=?",
                (project_store._now(), cohort_id),
            )
            conn.commit()
        finally:
            conn.close()
        return self.status(cohort_id)

    @serialized_project_authority_transition
    def seal(
        self,
        cohort_id: str,
        *,
        retain_maintenance_fence: bool = False,
        allow_unfenced_rehearsal: bool = False,
        rehearsal_authorization: IsolatedRehearsalAuthorization | None = None,
    ) -> dict[str, Any]:
        if retain_maintenance_fence and allow_unfenced_rehearsal:
            raise ProjectAuthorityError("Project seal modes are mutually exclusive")
        if allow_unfenced_rehearsal:
            require_isolated_rehearsal_path(
                project_store._db_path(),
                domain="projects",
                authorization=rehearsal_authorization,
            )
        elif rehearsal_authorization is not None:
            raise ProjectAuthorityError(
                "Project rehearsal authorization requires rehearsal mode"
            )
        # Verification is deliberately repeated immediately before the write
        # transaction; callers cannot seal a stale prepared inventory.
        self.verify(cohort_id)
        conn = project_store.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cohort = conn.execute(
                "SELECT * FROM project_import_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if cohort is None:
                raise ProjectAuthorityError("Unknown project import cohort")
            if cohort["state"] == "sealed":
                conn.rollback()
                prepare_domain_seal(
                    "projects", project_store._db_path(), cohort_id=cohort_id
                )
                confirm_domain_seal(
                    "projects", project_store._db_path(), cohort_id=cohort_id
                )
                return self.status(cohort_id)
            if cohort["state"] != "verified":
                raise ProjectAuthorityError("Project cohort is not verified")
            incomplete_source = conn.execute(
                "SELECT 1 FROM project_import_source_dependencies "
                "WHERE cohort_id=? AND source_usage_state!='acknowledged' LIMIT 1",
                (cohort_id,),
            ).fetchone()
            if incomplete_source is not None:
                raise ProjectAuthorityError(
                    "Project cohort has incomplete Source dependencies"
                )
            authority = conn.execute(
                "SELECT * FROM project_authority_state WHERE singleton=1"
            ).fetchone()
            if authority is None or authority["authority"] != "legacy_markdown":
                raise ProjectAuthorityError("Project authority cannot be sealed")
            maintenance = conn.execute(
                "SELECT state,cohort_id,inventory_sha256 FROM cutover_maintenance "
                "WHERE singleton=1"
            ).fetchone()
            if retain_maintenance_fence:
                if (
                    authority["state"] != "write_fenced"
                    or maintenance is None
                    or maintenance["state"] != "preseal_fenced"
                    or maintenance["cohort_id"] != cohort_id
                    or maintenance["inventory_sha256"] != cohort["inventory_sha256"]
                ):
                    raise ProjectAuthorityError(
                        "Project cutover maintenance is not held"
                    )
            elif maintenance is None or maintenance["state"] != "open":
                raise ProjectAuthorityError(
                    "Project cutover maintenance must remain held through release"
                )
            elif not allow_unfenced_rehearsal:
                raise ProjectAuthorityError(
                    "Project authority sealing requires preseal maintenance"
                )
            now = project_store._now()
            rows = conn.execute(
                "SELECT * FROM project_import_items "
                "WHERE cohort_id=? AND disposition='staged' ORDER BY relative_path",
                (cohort_id,),
            ).fetchall()
            imported = 0
            for item in rows:
                payload = json.loads(item["payload_json"])
                current = conn.execute(
                    "SELECT id,origin,created_at FROM projects WHERE slug=?",
                    (payload["slug"],),
                ).fetchone()
                if current is None:
                    cursor = conn.execute(
                        "INSERT INTO projects "
                        "(slug,name,status,description,origin,created_at,updated_at) "
                        "VALUES (?,?,?,?, 'vault',?,?)",
                        (
                            payload["slug"],
                            payload["name"],
                            payload["status"],
                            payload["description"],
                            now,
                            now,
                        ),
                    )
                    project_id = int(cursor.lastrowid)
                else:
                    project_id = int(current["id"])
                    project_store._require_plain_description(conn, project_id)
                    conn.execute(
                        "UPDATE projects SET name=?,status=?,description=?,updated_at=? "
                        "WHERE id=?",
                        (
                            payload["name"],
                            payload["status"],
                            payload["description"],
                            now,
                            project_id,
                        ),
                    )
                revision_id = project_store._write_revision(
                    conn,
                    project_id,
                    author="agent",
                    now=now,
                    change_summary=f"sealed legacy import cohort {cohort_id}",
                )
                conn.execute(
                    "INSERT INTO project_body_roles "
                    "(project_id,role,body_mode,document_binding_id,"
                    " interaction_contract_id,interaction_contract_version,"
                    " revision_id,privacy_class,updated_at) "
                    "VALUES (?,'description','plain',NULL,'project_description/v1',"
                    " 1,?,'private',?) "
                    "ON CONFLICT(project_id,role) DO UPDATE SET "
                    "body_mode='plain',document_binding_id=NULL,revision_id=excluded.revision_id,"
                    "updated_at=excluded.updated_at",
                    (project_id, revision_id, now),
                )
                content_sha = _sha_json(payload)
                conn.execute(
                    "INSERT OR IGNORE INTO project_outbox "
                    "(event_id,project_id,revision_id,event_kind,content_sha256,"
                    " privacy_class,committed_at) VALUES (?,?,?,?,?,'private',?)",
                    (
                        f"project:{project_id}:revision:{revision_id}",
                        project_id,
                        revision_id,
                        "project.imported",
                        content_sha,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO project_legacy_import_map "
                    "(cohort_id,relative_path,source_sha256,project_id,revision_id,"
                    " source_ref,parity_status,sealed_at) VALUES (?,?,?,?,?,?, 'exact',?)",
                    (
                        cohort_id,
                        item["relative_path"],
                        item["source_sha256"],
                        project_id,
                        revision_id,
                        item["source_ref"],
                        now,
                    ),
                )
                imported += 1
            conn.execute(
                "UPDATE project_import_cohorts SET state='sealed',sealed_at=? "
                "WHERE cohort_id=? AND state='verified'",
                (now, cohort_id),
            )
            if retain_maintenance_fence:
                mark_postseal_pending(
                    conn,
                    domain="projects",
                    cohort_id=cohort_id,
                    inventory_sha256=str(cohort["inventory_sha256"]),
                    at=now,
                )
            prepare_domain_seal(
                "projects", project_store._db_path(), cohort_id=cohort_id
            )
            conn.execute(
                "UPDATE project_authority_state SET authority='sqlite',"
                "authority_epoch=authority_epoch+1,state=?,"
                "sealed_cohort_id=?,sealed_at=?,updated_at=? WHERE singleton=1",
                (
                    "write_fenced" if retain_maintenance_fence else "active",
                    cohort_id,
                    now,
                    now,
                ),
            )
            conn.commit()
            confirm_domain_seal(
                "projects", project_store._db_path(), cohort_id=cohort_id
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        result = self.status(cohort_id)
        result["importedCount"] = imported
        return result

    def abort(self, cohort_id: str) -> dict[str, Any]:
        source_dependencies = self._require_source_dependencies(None)
        conn = project_store.get_connection()
        try:
            row = conn.execute(
                "SELECT state FROM project_import_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if row is None:
                raise ProjectAuthorityError("Unknown project import cohort")
            if row["state"] == "sealed":
                raise ProjectAuthorityError("A sealed cohort cannot be aborted")
            conn.execute(
                "UPDATE project_import_cohorts SET state='aborted',aborted_at=? "
                "WHERE cohort_id=?",
                (project_store._now(), cohort_id),
            )
            conn.commit()
        finally:
            conn.close()
        conn = project_store.get_connection()
        try:
            dependencies = conn.execute(
                "SELECT source_usage_id,source_usage_state "
                "FROM project_import_source_dependencies WHERE cohort_id=? "
                "AND source_usage_id IS NOT NULL",
                (cohort_id,),
            ).fetchall()
        finally:
            conn.close()
        for dependency in dependencies:
            if dependency["source_usage_state"] == "released":
                continue
            source_dependencies.release(str(dependency["source_usage_id"]))
            conn = project_store.get_connection()
            try:
                conn.execute(
                    "UPDATE project_import_source_dependencies SET "
                    "source_usage_state='released',released_at=? "
                    "WHERE cohort_id=? AND source_usage_id=?",
                    (
                        project_store._now(),
                        cohort_id,
                        dependency["source_usage_id"],
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return self.status(cohort_id)

    def _stage_sources(
        self,
        cohort_id: str,
        root: Path,
        context: TrustedIngressContext,
        service: ExactImportSourceService,
    ) -> None:
        conn = project_store.get_connection()
        try:
            rows = conn.execute(
                "SELECT d.*,i.source_sha256,i.byte_length "
                "FROM project_import_source_dependencies d "
                "JOIN project_import_items i USING(cohort_id,relative_path) "
                "WHERE d.cohort_id=? ORDER BY d.relative_path",
                (cohort_id,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            state = str(row["source_usage_state"])
            if state == "released":
                raise ProjectAuthorityError(
                    "a Project import Source dependency was released"
                )
            try:
                if row["source_ref"] is None:
                    raw = (root / str(row["relative_path"])).read_bytes()
                    if (
                        len(raw) != int(row["byte_length"])
                        or _sha_bytes(raw) != str(row["source_sha256"])
                    ):
                        raise ProjectAuthorityError(
                            "the Project source changed before Source retention"
                        )
                    binding = service.retain(
                        exact_content=raw,
                        client_mutation_id=str(row["ingress_client_mutation_id"]),
                        consumer_id=str(row["source_usage_consumer_id"]),
                        context=context,
                        source_committed=lambda _commit, key=str(row["relative_path"]): (
                            self._source_committed(cohort_id, key)
                        ),
                    )
                else:
                    binding = service.reconcile(
                        source_ref=str(row["source_ref"]),
                        representation_id=str(row["representation_id"]),
                        consumer_id=str(row["source_usage_consumer_id"]),
                        context=context,
                    )
                if row["source_usage_id"] is not None and str(
                    row["source_usage_id"]
                ) != binding.usage_id:
                    raise ProjectAuthorityError(
                        "a Project import Source dependency changed"
                    )
                now = project_store._now()
                conn = project_store.get_connection()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "UPDATE project_import_source_dependencies SET "
                        "source_ref=?,representation_id=?,"
                        "submission_id=COALESCE(?,submission_id),"
                        "source_usage_id=?,source_usage_state='reserved',"
                        "retained_at=COALESCE(retained_at,?) "
                        "WHERE cohort_id=? AND relative_path=? "
                        "AND source_usage_state IN ('unreserved','reserved')",
                        (
                            binding.source_ref,
                            binding.representation_id,
                            binding.submission_id,
                            binding.usage_id,
                            now,
                            cohort_id,
                            row["relative_path"],
                        ),
                    )
                    conn.execute(
                        "UPDATE project_import_items SET source_ref=? "
                        "WHERE cohort_id=? AND relative_path=?",
                        (binding.source_ref, cohort_id, row["relative_path"]),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
                service.acknowledge(binding.usage_id)
                conn = project_store.get_connection()
                try:
                    conn.execute(
                        "UPDATE project_import_source_dependencies SET "
                        "source_usage_state='acknowledged',"
                        "acknowledged_at=COALESCE(acknowledged_at,?) "
                        "WHERE cohort_id=? AND relative_path=? "
                        "AND source_usage_id=? AND source_usage_state "
                        "IN ('reserved','acknowledged')",
                        (now, cohort_id, row["relative_path"], binding.usage_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except ExactImportSourceError as exc:
                raise ProjectAuthorityError(str(exc)) from exc

    def _require_source_dependencies(
        self, context: TrustedIngressContext | None
    ) -> ExactImportSourceService:
        if self.source_dependencies is None:
            raise ProjectAuthorityError(
                "Project import requires an isolated Sources authority"
            )
        if context is None:
            return self.source_dependencies
        if IMPORT_SOURCE_PURPOSE not in context.permitted_purposes:
            raise ProjectAuthorityError(
                "trusted ingress does not permit Project history import"
            )
        return self.source_dependencies

    @staticmethod
    def _ingress_mutation_id(cohort_id: str, relative_path: str) -> str:
        digest = _sha_json({"cohortId": cohort_id, "relativePath": relative_path})
        return f"project-history-import:{digest}"

    @staticmethod
    def _source_consumer_id(cohort_id: str, relative_path: str) -> str:
        digest = _sha_json({"cohortId": cohort_id, "relativePath": relative_path})
        return f"project-import:{digest}"


def update_project_authoritatively(
    slug: str,
    fields: Mapping[str, Any],
    *,
    expected_revision_id: int | None = None,
    intent_id: str | None = None,
) -> dict[str, Any]:
    """Route a user edit to exactly one current authority."""

    if sqlite_authority_active():
        result = project_store.update_project(
            slug,
            author="user",
            change_summary="dashboard database-authority update",
            expected_revision_id=expected_revision_id,
            intent_id=intent_id,
            **dict(fields),
        )
        if result is None:
            raise ProjectAuthorityError(f"Project {slug!r} was not found")
        return result
    from work_buddy.markdown_db import WriteProvenance
    from work_buddy.projects.markdown_db import ProjectMarkdownDB

    ProjectMarkdownDB().apply_mutation(
        slug,
        dict(fields),
        provenance=WriteProvenance.mutation(frozenset({"user"}), "dashboard"),
    )
    result = project_store.get_project(slug)
    if result is None:
        raise ProjectAuthorityError(f"Project {slug!r} was not found")
    return result


def reconcile_projects_authoritatively() -> dict[str, Any]:
    """Run legacy reconciliation only before the SQLite authority seal."""

    if sqlite_authority_active():
        return {
            "status": "disabled",
            "reason": "projects_sqlite_authority",
            "writes": 0,
        }
    from work_buddy.projects.markdown_db import reconcile_projects

    return reconcile_projects()
