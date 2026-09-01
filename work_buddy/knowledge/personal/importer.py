"""Deterministic, inert importer for legacy personal Markdown.

Import is an operator action with four explicit phases: inventory/prepare,
verify, seal, or abort.  Prepared payloads live in hidden staging tables and
ordinary provider reads cannot observe them.  Seal publishes every staged unit
and the authority receipt in one SQLite transaction.  No phase writes the
source tree, invokes an agent, or updates live configuration.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

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
from work_buddy.knowledge.personal.store import (
    VALID_DISCLOSURE_CLASSES,
    VALID_PRIVACY_CLASSES,
    PersonalKnowledgeConflict,
    PersonalKnowledgeStore,
    canonical_json,
    normalize_logical_path,
    sha256_json,
    utcnow,
)
from work_buddy.installed_authority import (
    confirm_domain_seal,
    mark_domain_released,
    prepare_domain_seal,
)
from work_buddy.knowledge.vault_adapter import _as_list, _extract_summary, _first_sentence
from work_buddy.sources import SourceStore, TrustedIngressContext
from work_buddy.sources.import_dependency import (
    ExactImportSourceError,
    ExactImportSourceService,
)


PARSER_VERSION = "wb.personal-knowledge-markdown-import/v1"
IMPORT_SOURCE_PURPOSE = "personal_knowledge.history_import"
IMPORT_SOURCE_USE_KIND = "personal_knowledge_history_import"
_IMPORT_NAMESPACE = uuid.UUID("a44a0dc4-6554-4c6d-a80e-a19833d32fe8")
_EVIDENCE_RE = re.compile(
    r"^\s*[*-]\s+(?:(\d{4}-\d{2}-\d{2})\s+-\s+)?(.+?)\s*$",
    re.MULTILINE,
)
_EVIDENCE_SECTION_RE = re.compile(
    r"^##\s+Evidence\s*\n(.*?)(?=\n##\s|\Z)", re.MULTILINE | re.DOTALL
)
_REFERENCE_RE = re.compile(r"<<wb:([^>\s]+)(?:\s+[^>]*)?>>")


class PersonalKnowledgeImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ImportItem:
    relative_path: str
    byte_length: int
    mtime_ns: int
    source_sha256: str
    logical_path: str | None
    unit_id: str | None
    payload: Mapping[str, Any] | None
    disposition: str
    reason_code: str | None
    parity_status: str | None

    def receipt(self) -> dict[str, Any]:
        return {
            "relativePath": self.relative_path,
            "byteLength": self.byte_length,
            "sourceSha256": self.source_sha256,
            "logicalPath": self.logical_path,
            "unitId": self.unit_id,
            "disposition": self.disposition,
            "reasonCode": self.reason_code,
            "parityStatus": self.parity_status,
        }


def _root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise PersonalKnowledgeImportError("personal knowledge source root is unavailable")
    return root


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise ValueError("missing_frontmatter")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise ValueError("malformed_frontmatter")
    try:
        data = yaml.safe_load(normalized[4:end]) or {}
    except yaml.YAMLError as exc:
        raise ValueError("malformed_frontmatter") from exc
    if not isinstance(data, dict):
        raise ValueError("malformed_frontmatter")
    return data, normalized[end + 5 :].lstrip("\n")


def _observations(body: str) -> list[dict[str, str]]:
    match = _EVIDENCE_SECTION_RE.search(body)
    if match is None:
        return []
    result: list[dict[str, str]] = []
    for row in _EVIDENCE_RE.finditer(match.group(1)):
        evidence = row.group(2).strip()
        if not evidence or evidence.casefold() == "no observations yet.":
            continue
        result.append({"observed_at": row.group(1) or "", "evidence": evidence})
    return result


def _parse_item(root: Path, path: Path) -> ImportItem:
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_file():
        raise PersonalKnowledgeImportError("personal import accepts regular files only")
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise PersonalKnowledgeImportError("personal import input escaped its root") from exc
    raw = resolved.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    stat = resolved.stat()
    base = {
        "relative_path": relative,
        "byte_length": len(raw),
        "mtime_ns": int(stat.st_mtime_ns),
        "source_sha256": digest,
    }
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ImportItem(**base, logical_path=None, unit_id=None, payload=None,
                          disposition="quarantined", reason_code="invalid_utf8",
                          parity_status=None)
    try:
        frontmatter, body = _frontmatter(text)
    except ValueError as exc:
        return ImportItem(**base, logical_path=None, unit_id=None, payload=None,
                          disposition="quarantined", reason_code=str(exc),
                          parity_status=None)
    name = str(frontmatter.get("name") or "").strip()
    if not name:
        return ImportItem(**base, logical_path=None, unit_id=None, payload=None,
                          disposition="quarantined", reason_code="missing_name",
                          parity_status=None)
    logical_path = normalize_logical_path(
        "personal/" + Path(relative).with_suffix("").as_posix()
    )
    unit_id = str(uuid.uuid5(_IMPORT_NAMESPACE, logical_path.casefold()))
    categories = _as_list(frontmatter.get("categories", []))
    primary = str(frontmatter.get("category") or "").strip()
    if primary and primary not in categories:
        categories.insert(0, primary)
    privacy = str(
        frontmatter.get("privacy_class", frontmatter.get("privacy", "private"))
        or "private"
    )
    disclosure = str(
        frontmatter.get(
            "disclosure_class", frontmatter.get("disclosure", "local_only")
        )
        or "local_only"
    )
    if privacy not in VALID_PRIVACY_CLASSES or disclosure not in VALID_DISCLOSURE_CLASSES:
        return ImportItem(**base, logical_path=logical_path, unit_id=unit_id,
                          payload=None, disposition="quarantined",
                          reason_code="invalid_privacy_metadata", parity_status=None)
    parent_paths: list[str] = []
    try:
        parent_paths = [normalize_logical_path(v) for v in _as_list(frontmatter.get("parents"))]
        children = [normalize_logical_path(v) for v in _as_list(frontmatter.get("children"))]
        references = [
            normalize_logical_path(v)
            for v in [*_as_list(frontmatter.get("references")), *_REFERENCE_RE.findall(body)]
            if str(v).startswith("personal/")
        ]
        observation_count = int(frontmatter.get("observation_count", 0) or 0)
    except (TypeError, ValueError):
        return ImportItem(**base, logical_path=logical_path, unit_id=unit_id,
                          payload=None, disposition="quarantined",
                          reason_code="invalid_metadata", parity_status=None)
    payload: dict[str, Any] = {
        "unit_id": unit_id,
        "logical_path": logical_path,
        "name": name,
        "description": str(
            frontmatter.get("description") or _first_sentence(_extract_summary(body))
        ),
        "summary": _extract_summary(body),
        "body": body,
        "body_mode": "plain",
        "categories": categories,
        "aliases": _as_list(frontmatter.get("aliases")),
        "tags": _as_list(frontmatter.get("tags")),
        "requires": _as_list(frontmatter.get("requires")),
        "parent_paths": parent_paths,
        "declared_children": children,
        "reference_paths": references,
        "severity": str(frontmatter.get("severity") or ""),
        "privacy_class": privacy,
        "disclosure_class": disclosure,
        "observation_count": max(0, observation_count),
        "last_observed": str(frontmatter.get("last_observed") or ""),
        "source_file": relative,
        "observations": _observations(body),
    }
    parity = "normalized" if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw else "exact"
    return ImportItem(
        **base,
        logical_path=logical_path,
        unit_id=unit_id,
        payload=payload,
        disposition="staged",
        reason_code=None,
        parity_status=parity,
    )


def inventory_personal_markdown(source_root: str | Path) -> tuple[ImportItem, ...]:
    root = _root(source_root)
    items = [_parse_item(root, path) for path in sorted(root.rglob("*.md"))]
    # File systems differ in case sensitivity.  Treat paths that collide after
    # case folding as ambiguous and quarantine every member of the collision.
    by_path: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        if item.logical_path:
            by_path.setdefault(item.logical_path.casefold(), []).append(index)
    for indexes in by_path.values():
        if len(indexes) > 1:
            for index in indexes:
                items[index] = replace(
                    items[index], payload=None, disposition="quarantined",
                    reason_code="logical_path_collision", parity_status=None,
                )
    # Translate legacy children declarations into the child's parent list so
    # the canonical edge direction remains child -> parent.
    path_to_index = {
        item.logical_path: index
        for index, item in enumerate(items)
        if item.disposition == "staged" and item.logical_path and item.payload
    }
    for parent in list(items):
        if parent.disposition != "staged" or not parent.payload:
            continue
        for child_path in parent.payload.get("declared_children", []):
            index = path_to_index.get(child_path)
            if index is None:
                continue
            child = items[index]
            payload = dict(child.payload or {})
            parents = list(payload.get("parent_paths", []))
            if parent.logical_path not in parents:
                parents.append(parent.logical_path)
            payload["parent_paths"] = parents
            items[index] = replace(child, payload=payload)
    for index, item in enumerate(items):
        if item.payload:
            payload = dict(item.payload)
            payload.pop("declared_children", None)
            items[index] = replace(item, payload=payload)
    return tuple(items)


def _inventory_digest(items: tuple[ImportItem, ...]) -> str:
    return sha256_json(
        {"parserVersion": PARSER_VERSION, "items": [item.receipt() for item in items]}
    )


class PersonalKnowledgeImportCoordinator:
    def __init__(
        self,
        store: PersonalKnowledgeStore | None = None,
        source_store: SourceStore | None = None,
        source_committed: Callable[[str, str], None] | None = None,
    ) -> None:
        self.store = store or PersonalKnowledgeStore()
        self.sources = source_store
        self.source_dependencies = (
            ExactImportSourceService(
                source_store,
                purpose=IMPORT_SOURCE_PURPOSE,
                consumer_domain="personal_knowledge",
                use_kind=IMPORT_SOURCE_USE_KIND,
            )
            if source_store is not None
            else None
        )
        self._source_committed = source_committed or (
            lambda _cohort_id, _relative_path: None
        )

    def pause_mutations(
        self,
        *,
        cohort_id: str,
        inventory_sha256: str,
        mutation_id: str,
        actor: str,
    ) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authority = conn.execute(
                "SELECT authority FROM personal_knowledge_authority WHERE singleton=1"
            ).fetchone()
            if authority is None or authority["authority"] != "legacy_markdown":
                raise PersonalKnowledgeImportError(
                    "personal knowledge cannot enter preseal maintenance"
                )
            result = pause_cutover_maintenance(
                conn,
                domain="personal_knowledge",
                cohort_id=cohort_id,
                inventory_sha256=inventory_sha256,
                mutation_id=mutation_id,
                actor_sha256=sha256_json({"actor": actor}),
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def resume_preseal_mutations(
        self,
        *,
        cohort_id: str,
        mutation_id: str,
        actor: str,
    ) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authority = conn.execute(
                "SELECT authority FROM personal_knowledge_authority WHERE singleton=1"
            ).fetchone()
            if authority is None or authority["authority"] != "legacy_markdown":
                raise PersonalKnowledgeImportError(
                    "sealed personal knowledge maintenance cannot resume"
                )
            result = resume_preseal_maintenance(
                conn,
                domain="personal_knowledge",
                cohort_id=cohort_id,
                mutation_id=mutation_id,
                actor_sha256=sha256_json({"actor": actor}),
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
        conn = self.store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authority = conn.execute(
                "SELECT authority,sealed_cohort_id FROM personal_knowledge_authority "
                "WHERE singleton=1"
            ).fetchone()
            if (
                authority is None
                or authority["authority"] != "sqlite"
                or authority["sealed_cohort_id"] != cohort_id
            ):
                raise PersonalKnowledgeImportError(
                    "personal knowledge postseal maintenance is unavailable"
                )
            prior_evidence = prior_postseal_release_evidence(
                conn, mutation_id=mutation_id
            )
            if allow_unvalidated_rehearsal:
                require_isolated_rehearsal_path(
                    self.store.db_path,
                    domain="personal_knowledge",
                    authorization=rehearsal_authorization,
                )
                if rehearsal_evidence_sha256s is None:
                    raise PersonalKnowledgeImportError(
                        "personal knowledge rehearsal evidence is missing"
                    )
                evidence = dict(rehearsal_evidence_sha256s)
                if set(evidence) != {"databaseCheckpoint", "search", "detachment"}:
                    raise CutoverMaintenanceError("postseal evidence is incomplete")
                evidence["authorityHead"] = (
                    prior_evidence["authorityHead"]
                    if prior_evidence is not None
                    else hashlib.sha256(self.store.db_path.read_bytes()).hexdigest()
                )
            else:
                if (
                    checkpoint_evidence_path is None
                    or search_evidence_path is None
                    or detachment_evidence_path is None
                    or rehearsal_evidence_sha256s is not None
                    or rehearsal_authorization is not None
                ):
                    raise PersonalKnowledgeImportError(
                        "personal knowledge configured postseal evidence is required"
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
                        domain="personal_knowledge",
                        authority_db_path=self.store.db_path,
                        checkpoint_evidence_path=checkpoint_evidence_path,
                        search_evidence_path=search_evidence_path,
                        detachment_evidence_path=detachment_evidence_path,
                    )
            result = release_postseal_maintenance(
                conn,
                domain="personal_knowledge",
                cohort_id=cohort_id,
                mutation_id=mutation_id,
                actor_sha256=sha256_json({"actor": actor}),
                evidence_sha256s=evidence,
            )
            conn.commit()
            mark_domain_released(
                "personal_knowledge", self.store.db_path, cohort_id=cohort_id
            )
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def status(self, cohort_id: str, *, include_items: bool = True) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM personal_import_cohorts WHERE cohort_id=?", (cohort_id,)
            ).fetchone()
            if row is None:
                raise PersonalKnowledgeImportError("unknown personal import cohort")
            result = dict(row)
            if include_items:
                result["items"] = [
                    {
                        key: item[key]
                        for key in (
                            "relative_path", "source_sha256", "byte_length", "logical_path",
                            "unit_id", "disposition", "reason_code", "parity_status",
                            "source_ref",
                        )
                    }
                    for item in conn.execute(
                        "SELECT * FROM personal_import_items WHERE cohort_id=? "
                        "ORDER BY relative_path",
                        (cohort_id,),
                    ).fetchall()
                ]
            receipt = conn.execute(
                "SELECT result_json,result_sha256 FROM personal_import_receipts "
                "WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if receipt is not None:
                result["receipt"] = json.loads(receipt["result_json"])
                result["receipt_sha256"] = receipt["result_sha256"]
            source_counts = conn.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN source_usage_state='acknowledged' THEN 1 ELSE 0 END) "
                "AS acknowledged FROM personal_import_source_dependencies "
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
            raise PersonalKnowledgeImportError(
                "caller-supplied personal Source references are not accepted"
            )
        # A sealed replay is answered entirely from receipts: it does not even
        # stat the retired source root.
        conn = self.store.connect()
        try:
            existing = conn.execute(
                "SELECT state FROM personal_import_cohorts WHERE cohort_id=?", (cohort_id,)
            ).fetchone()
        finally:
            conn.close()
        if existing is not None and existing["state"] == "sealed":
            return self.status(cohort_id)

        if ingress_context is None:
            raise PersonalKnowledgeImportError(
                "personal import requires trusted ingress context"
            )
        source_dependencies = self._require_source_dependencies(ingress_context)
        root = _root(source_root)
        items = inventory_personal_markdown(root)
        inventory_sha = _inventory_digest(items)
        request_sha = sha256_json(
            {
                "cohortId": cohort_id,
                "parserVersion": PARSER_VERSION,
                "inventorySha256": inventory_sha,
                "sourceRetention": "exact-source/v1",
            }
        )
        conn = self.store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            authority = conn.execute(
                "SELECT sealed_cohort_id FROM personal_knowledge_authority WHERE singleton=1"
            ).fetchone()
            if authority is None:
                raise PersonalKnowledgeImportError("personal authority state is missing")
            if authority["sealed_cohort_id"] and authority["sealed_cohort_id"] != cohort_id:
                raise PersonalKnowledgeImportError(
                    "a personal knowledge import cohort has already been sealed"
                )
            prior = conn.execute(
                "SELECT request_sha256 FROM personal_import_cohorts WHERE cohort_id=?",
                (cohort_id,),
            ).fetchone()
            if prior is not None:
                if prior["request_sha256"] != request_sha:
                    raise PersonalKnowledgeImportError(
                        "cohort_id was already used for a different frozen inventory"
                    )
                conn.rollback()
            else:
                staged = sum(item.disposition == "staged" for item in items)
                quarantined = len(items) - staged
                conn.execute(
                "INSERT INTO personal_import_cohorts "
                "(cohort_id,parser_version,request_sha256,inventory_sha256,source_root,"
                "state,file_count,staged_count,quarantined_count,prepared_at) "
                "VALUES (?,?,?,?,?,'prepared',?,?,?,?)",
                (
                    cohort_id, PARSER_VERSION, request_sha, inventory_sha, str(root),
                    len(items), staged, quarantined, utcnow(),
                ),
            )
                for item in items:
                    conn.execute(
                    "INSERT INTO personal_import_items "
                    "(cohort_id,relative_path,source_sha256,byte_length,mtime_ns,"
                    "logical_path,unit_id,payload_json,disposition,reason_code,"
                    "source_ref,parity_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cohort_id, item.relative_path, item.source_sha256,
                        item.byte_length, item.mtime_ns, item.logical_path, item.unit_id,
                        canonical_json(item.payload) if item.payload is not None else None,
                        item.disposition, item.reason_code, None,
                        item.parity_status,
                    ),
                    )
                    conn.execute(
                        "INSERT INTO personal_import_source_dependencies "
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

    def verify(self, cohort_id: str) -> dict[str, Any]:
        status = self.status(cohort_id, include_items=False)
        if status["state"] == "sealed":
            prepare_domain_seal(
                "personal_knowledge", self.store.db_path, cohort_id=cohort_id
            )
            confirm_domain_seal(
                "personal_knowledge", self.store.db_path, cohort_id=cohort_id
            )
            return self.status(cohort_id)
        if status["state"] == "aborted":
            raise PersonalKnowledgeImportError("an aborted cohort cannot be verified")
        conn = self.store.connect()
        try:
            dependencies = conn.execute(
                "SELECT d.*,i.source_sha256,i.byte_length "
                "FROM personal_import_source_dependencies d "
                "JOIN personal_import_items i USING(cohort_id,relative_path) "
                "WHERE d.cohort_id=? ORDER BY d.relative_path",
                (cohort_id,),
            ).fetchall()
        finally:
            conn.close()
        if len(dependencies) != int(status["file_count"]) or any(
            row["source_usage_state"] != "acknowledged" for row in dependencies
        ):
            raise PersonalKnowledgeImportError(
                "personal cohort has incomplete Source dependencies"
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
                raise PersonalKnowledgeImportError(str(exc)) from exc
        actual = inventory_personal_markdown(status["source_root"])
        if _inventory_digest(actual) != status["inventory_sha256"]:
            raise PersonalKnowledgeImportError("the frozen personal knowledge corpus changed")
        if status["quarantined_count"]:
            raise PersonalKnowledgeImportError(
                "personal knowledge cohort contains quarantined files"
            )
        conn = self.store.connect()
        try:
            conn.execute(
                "UPDATE personal_import_cohorts SET state='verified',verified_at=? "
                "WHERE cohort_id=? AND state IN ('prepared','verified')",
                (utcnow(), cohort_id),
            )
            conn.commit()
        finally:
            conn.close()
        return self.status(cohort_id)

    def seal(
        self,
        cohort_id: str,
        *,
        retain_maintenance_fence: bool = False,
        allow_unfenced_rehearsal: bool = False,
        rehearsal_authorization: IsolatedRehearsalAuthorization | None = None,
    ) -> dict[str, Any]:
        if retain_maintenance_fence and allow_unfenced_rehearsal:
            raise PersonalKnowledgeImportError(
                "personal knowledge seal modes are mutually exclusive"
            )
        if allow_unfenced_rehearsal:
            require_isolated_rehearsal_path(
                self.store.db_path,
                domain="personal_knowledge",
                authorization=rehearsal_authorization,
            )
        elif rehearsal_authorization is not None:
            raise PersonalKnowledgeImportError(
                "personal knowledge rehearsal authorization requires rehearsal mode"
            )
        status = self.status(cohort_id, include_items=False)
        if status["state"] == "sealed":
            prepare_domain_seal(
                "personal_knowledge", self.store.db_path, cohort_id=cohort_id
            )
            confirm_domain_seal(
                "personal_knowledge", self.store.db_path, cohort_id=cohort_id
            )
            return self.status(cohort_id)
        # Re-freeze immediately before taking the publication transaction.
        self.verify(cohort_id)
        conn = self.store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cohort = conn.execute(
                "SELECT * FROM personal_import_cohorts WHERE cohort_id=?", (cohort_id,)
            ).fetchone()
            if cohort is None or cohort["state"] != "verified":
                raise PersonalKnowledgeImportError("cohort is not verified")
            incomplete_source = conn.execute(
                "SELECT 1 FROM personal_import_source_dependencies "
                "WHERE cohort_id=? AND source_usage_state!='acknowledged' LIMIT 1",
                (cohort_id,),
            ).fetchone()
            if incomplete_source is not None:
                raise PersonalKnowledgeImportError(
                    "personal cohort has incomplete Source dependencies"
                )
            authority = conn.execute(
                "SELECT * FROM personal_knowledge_authority WHERE singleton=1"
            ).fetchone()
            if authority is None:
                raise PersonalKnowledgeImportError("personal authority state is missing")
            if authority["sealed_cohort_id"] not in (None, cohort_id):
                raise PersonalKnowledgeImportError("another import cohort is already sealed")
            maintenance = conn.execute(
                "SELECT state,cohort_id,inventory_sha256 FROM cutover_maintenance "
                "WHERE singleton=1"
            ).fetchone()
            if retain_maintenance_fence:
                if (
                    maintenance is None
                    or maintenance["state"] != "preseal_fenced"
                    or maintenance["cohort_id"] != cohort_id
                    or maintenance["inventory_sha256"] != cohort["inventory_sha256"]
                ):
                    raise PersonalKnowledgeImportError(
                        "personal knowledge cutover maintenance is not held"
                    )
            elif maintenance is None or maintenance["state"] != "open":
                raise PersonalKnowledgeImportError(
                    "personal knowledge cutover maintenance must remain held through release"
                )
            elif not allow_unfenced_rehearsal:
                raise PersonalKnowledgeImportError(
                    "personal knowledge authority sealing requires preseal maintenance"
                )
            rows = conn.execute(
                "SELECT * FROM personal_import_items WHERE cohort_id=? "
                "AND disposition='staged' ORDER BY relative_path",
                (cohort_id,),
            ).fetchall()
            now = utcnow()
            imported = 0
            imported_results: list[dict[str, Any]] = []
            for row in rows:
                if conn.execute(
                    "SELECT 1 FROM personal_unit_paths WHERE logical_path=?",
                    (row["logical_path"],),
                ).fetchone() is not None:
                    raise PersonalKnowledgeConflict(
                        f"logical path already exists: {row['logical_path']}"
                    )
                payload = json.loads(row["payload_json"])
                result = self.store.insert_imported_unit(
                    conn, payload, cohort_id=cohort_id, source_ref=row["source_ref"]
                )
                conn.execute(
                    "INSERT INTO personal_import_map "
                    "(cohort_id,relative_path,source_sha256,unit_id,revision,logical_path,"
                    "source_ref,parity_status,sealed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        cohort_id, row["relative_path"], row["source_sha256"],
                        result["unit_id"], result["revision"], result["path"],
                        row["source_ref"], row["parity_status"] or "normalized", now,
                    ),
                )
                conn.execute(
                    "UPDATE personal_import_items SET disposition='sealed' "
                    "WHERE cohort_id=? AND relative_path=?",
                    (cohort_id, row["relative_path"]),
                )
                imported += 1
                imported_results.append(result)
            # All target identities now exist. Refresh the still-unpublished
            # revision/outbox projections so forward references and derived
            # child edges are represented in the sealed snapshot.
            self.store._resolve_pending_edges(conn)
            for imported_result in imported_results:
                record = self.store._record(conn, imported_result["unit_id"])
                snapshot_json = canonical_json(self.store._snapshot(record))
                conn.execute(
                    "UPDATE personal_unit_revisions SET snapshot_json=?,snapshot_sha256=? "
                    "WHERE unit_id=? AND revision=1",
                    (
                        snapshot_json,
                        hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
                        imported_result["unit_id"],
                    ),
                )
                conn.execute(
                    "UPDATE personal_search_outbox SET content_sha256=? "
                    "WHERE unit_id=? AND revision=1",
                    (
                        self.store._content_hash(record), imported_result["unit_id"]
                    ),
                )
            receipt_payload = {
                "schema": "wb.personal-knowledge-import-receipt/v1",
                "cohortId": cohort_id,
                "parserVersion": cohort["parser_version"],
                "inventorySha256": cohort["inventory_sha256"],
                "requestSha256": cohort["request_sha256"],
                "importedCount": imported,
                "mappings": [
                    {
                        "relativePath": row["relative_path"],
                        "sourceSha256": row["source_sha256"],
                        "unitId": row["unit_id"],
                        "logicalPath": row["logical_path"],
                        "parityStatus": row["parity_status"],
                    }
                    for row in conn.execute(
                        "SELECT relative_path,source_sha256,unit_id,logical_path,"
                        "parity_status FROM personal_import_map WHERE cohort_id=? "
                        "ORDER BY relative_path",
                        (cohort_id,),
                    ).fetchall()
                ],
            }
            receipt_json = canonical_json(receipt_payload)
            conn.execute(
                "INSERT INTO personal_import_receipts "
                "(cohort_id,request_sha256,result_sha256,result_json,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    cohort_id, cohort["request_sha256"],
                    hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
                    receipt_json, now,
                ),
            )
            conn.execute(
                "UPDATE personal_import_cohorts SET state='sealed',sealed_at=? "
                "WHERE cohort_id=?",
                (now, cohort_id),
            )
            if retain_maintenance_fence:
                mark_postseal_pending(
                    conn,
                    domain="personal_knowledge",
                    cohort_id=cohort_id,
                    inventory_sha256=str(cohort["inventory_sha256"]),
                    at=now,
                )
            prepare_domain_seal(
                "personal_knowledge", self.store.db_path, cohort_id=cohort_id
            )
            conn.execute(
                "UPDATE personal_knowledge_authority SET sealed_cohort_id=?,sealed_at=?,"
                "authority='sqlite',authority_epoch=authority_epoch+1,updated_at=? "
                "WHERE singleton=1 AND authority='legacy_markdown'",
                (cohort_id, now, now),
            )
            conn.commit()
            confirm_domain_seal(
                "personal_knowledge", self.store.db_path, cohort_id=cohort_id
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        result = self.status(cohort_id)
        result["imported_count"] = imported
        from work_buddy.knowledge.personal.provider import (
            set_personal_knowledge_provider,
        )
        from work_buddy.knowledge.store import invalidate_vault

        set_personal_knowledge_provider(None)
        invalidate_vault()
        return result

    def abort(self, cohort_id: str) -> dict[str, Any]:
        status = self.status(cohort_id, include_items=False)
        if status["state"] == "sealed":
            raise PersonalKnowledgeImportError("a sealed cohort cannot be aborted")
        source_dependencies = self._require_source_dependencies(None)
        conn = self.store.connect()
        try:
            conn.execute(
                "UPDATE personal_import_cohorts SET state='aborted',aborted_at=? "
                "WHERE cohort_id=?",
                (utcnow(), cohort_id),
            )
            conn.commit()
        finally:
            conn.close()
        conn = self.store.connect()
        try:
            dependencies = conn.execute(
                "SELECT source_usage_id,source_usage_state "
                "FROM personal_import_source_dependencies WHERE cohort_id=? "
                "AND source_usage_id IS NOT NULL",
                (cohort_id,),
            ).fetchall()
        finally:
            conn.close()
        for dependency in dependencies:
            if dependency["source_usage_state"] == "released":
                continue
            source_dependencies.release(str(dependency["source_usage_id"]))
            conn = self.store.connect()
            try:
                conn.execute(
                    "UPDATE personal_import_source_dependencies SET "
                    "source_usage_state='released',released_at=? "
                    "WHERE cohort_id=? AND source_usage_id=?",
                    (utcnow(), cohort_id, dependency["source_usage_id"]),
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
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT d.*,i.source_sha256,i.byte_length "
                "FROM personal_import_source_dependencies d "
                "JOIN personal_import_items i USING(cohort_id,relative_path) "
                "WHERE d.cohort_id=? ORDER BY d.relative_path",
                (cohort_id,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            if row["source_usage_state"] == "released":
                raise PersonalKnowledgeImportError(
                    "a personal import Source dependency was released"
                )
            try:
                if row["source_ref"] is None:
                    raw = (root / str(row["relative_path"])).read_bytes()
                    if (
                        len(raw) != int(row["byte_length"])
                        or hashlib.sha256(raw).hexdigest() != str(row["source_sha256"])
                    ):
                        raise PersonalKnowledgeImportError(
                            "the personal source changed before Source retention"
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
                    raise PersonalKnowledgeImportError(
                        "a personal import Source dependency changed"
                    )
                now = utcnow()
                conn = self.store.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "UPDATE personal_import_source_dependencies SET "
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
                        "UPDATE personal_import_items SET source_ref=? "
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
                conn = self.store.connect()
                try:
                    conn.execute(
                        "UPDATE personal_import_source_dependencies SET "
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
                raise PersonalKnowledgeImportError(str(exc)) from exc

    def _require_source_dependencies(
        self, context: TrustedIngressContext | None
    ) -> ExactImportSourceService:
        if self.source_dependencies is None:
            raise PersonalKnowledgeImportError(
                "personal import requires an isolated Sources authority"
            )
        if context is not None and IMPORT_SOURCE_PURPOSE not in context.permitted_purposes:
            raise PersonalKnowledgeImportError(
                "trusted ingress does not permit personal history import"
            )
        return self.source_dependencies

    @staticmethod
    def _ingress_mutation_id(cohort_id: str, relative_path: str) -> str:
        digest = sha256_json({"cohortId": cohort_id, "relativePath": relative_path})
        return f"personal-history-import:{digest}"

    @staticmethod
    def _source_consumer_id(cohort_id: str, relative_path: str) -> str:
        digest = sha256_json({"cohortId": cohort_id, "relativePath": relative_path})
        return f"personal-import:{digest}"
