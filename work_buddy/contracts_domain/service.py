"""Revisioned service API for the Contracts SQLite authority."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from work_buddy.contracts_domain.store import ContractStore
from work_buddy.cutover_maintenance import require_mutations_open


VALID_STATUSES = frozenset({"draft", "active", "paused", "completed", "abandoned"})
VALID_PRIVACY = frozenset({"private", "sensitive", "shared"})


class ContractValidationError(ValueError):
    pass


class ContractNotFound(LookupError):
    pass


class ContractConflict(RuntimeError):
    pass


class IdempotencyConflict(ContractConflict):
    pass


class WipLimitExceeded(ContractConflict):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(domain: str, value: Any) -> str:
    return hashlib.sha256(
        f"{domain}\0{_canonical_json(value)}".encode("utf-8")
    ).hexdigest()[:32]


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractValidationError(f"{label} must be text")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ContractValidationError(f"{label} is required")
    return value if allow_empty else normalized


def _actor(value: Any) -> str:
    return _text(value, "actor")


def _intent(value: Any) -> str:
    return _text(value, "intent_id")


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ContractValidationError(f"unsupported structured value: {type(value).__name__}")


def normalize_alias(value: str) -> str:
    alias = _text(value, "alias").replace("\\", "/")
    while alias.startswith("./"):
        alias = alias[2:]
    normalized = PurePosixPath(alias).as_posix().strip("/").casefold()
    if not normalized or normalized in {".", ".."} or normalized.startswith("../"):
        raise ContractValidationError("alias must be a contained logical name")
    return normalized


def _date_value(value: Any, label: str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{label} must be an ISO date")
    raw = value.strip()
    try:
        if "T" in raw:
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            date.fromisoformat(raw)
    except ValueError as exc:
        raise ContractValidationError(f"{label} must be an ISO date") from exc
    return raw


def _aliases(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ContractValidationError("aliases must be a list")
    result: dict[str, dict[str, str]] = {}
    for item in value:
        if isinstance(item, str):
            display = item
            kind = "user_alias"
        elif isinstance(item, Mapping):
            display = _text(item.get("alias", item.get("alias_display")), "alias")
            kind = str(item.get("kind", item.get("alias_kind", "user_alias")))
        else:
            raise ContractValidationError("alias entries must be text or objects")
        if kind not in {"logical_name", "legacy_path", "user_alias"}:
            raise ContractValidationError("alias kind is invalid")
        key = normalize_alias(display)
        result[key] = {"alias_key": key, "alias_display": display, "alias_kind": kind}
    return [result[key] for key in sorted(result)]


def _dates(payload: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    raw = payload.get("dates", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ContractValidationError("dates must be an object")
    combined = dict(raw)
    for key in ("deadline", "last_reviewed", "start_date", "completed_at"):
        if key in payload:
            if payload[key] is None or payload[key] == "":
                combined.pop(key, None)
            else:
                combined[key] = payload[key]
    result: dict[str, dict[str, str]] = {}
    for kind, item in combined.items():
        if isinstance(item, Mapping):
            value = item.get("value", item.get("date_value"))
            precision = str(item.get("precision", "day"))
        else:
            value = item
            precision = "datetime" if isinstance(value, str) and "T" in value else "day"
        if precision not in {"day", "month", "year", "datetime"}:
            raise ContractValidationError(f"date {kind!r} precision is invalid")
        result[str(kind)] = {
            "value": _date_value(value, f"date {kind!r}"),
            "precision": precision,
        }
    return {key: result[key] for key in sorted(result)}


def _structured_list(
    contract_id: str,
    value: Any,
    *,
    kind: str,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ContractValidationError(f"{kind} must be a list")
    result: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(value):
        if kind in {"commitments", "constraints"} and isinstance(raw, str):
            item: dict[str, Any] = {"text": raw}
        elif isinstance(raw, Mapping):
            item = dict(_json_safe(raw))
        else:
            raise ContractValidationError(f"{kind} entries must be objects")
        if kind == "commitments":
            text = _text(item.get("text", item.get("task")), "commitment text")
            state = str(item.get("state", "done" if item.get("done") else "open"))
            if state not in {"open", "done", "waived"}:
                raise ContractValidationError("commitment state is invalid")
            due = item.get("due_date")
            normalized = {
                "commitment_id": _stable_id(
                    "contract-commitment", [contract_id, ordinal, text]
                ),
                "kind": str(item.get("kind", "deliverable")),
                "text": text,
                "state": state,
                "due_date": None if due in {None, ""} else _date_value(due, "due_date"),
                "ordinal": ordinal,
            }
        elif kind == "constraints":
            text = _text(item.get("text"), "constraint text")
            state = str(item.get("state", "current"))
            if state not in {"current", "resolved", "superseded"}:
                raise ContractValidationError("constraint state is invalid")
            normalized = {
                "constraint_id": _stable_id(
                    "contract-constraint", [contract_id, ordinal, text]
                ),
                "kind": str(item.get("kind", "current")),
                "text": text,
                "state": state,
                "ordinal": ordinal,
            }
        elif kind == "participants":
            entity_ref = item.get("entity_ref")
            display = item.get("display_name", item.get("name"))
            if entity_ref is None and display is None:
                raise ContractValidationError("participant needs entity_ref or display_name")
            normalized = {
                "participant_id": _stable_id(
                    "contract-participant", [contract_id, ordinal, entity_ref, display]
                ),
                "entity_ref": None if entity_ref is None else _text(entity_ref, "entity_ref"),
                "display_name": None if display is None else _text(display, "display_name"),
                "role": str(item.get("role", "participant")),
                "ordinal": ordinal,
            }
        else:
            reference = _text(
                item.get("evidence_ref", item.get("ref", item.get("task"))),
                "evidence_ref",
            )
            requirement = str(item.get("requirement", "must_have"))
            state = str(item.get("state", "satisfied" if item.get("done") else "open"))
            if requirement not in {"must_have", "optional"}:
                raise ContractValidationError("evidence requirement is invalid")
            if state not in {"open", "satisfied", "waived"}:
                raise ContractValidationError("evidence state is invalid")
            normalized = {
                "evidence_link_id": _stable_id(
                    "contract-evidence", [contract_id, ordinal, reference]
                ),
                "evidence_ref": reference,
                "label": item.get("label"),
                "requirement": requirement,
                "state": state,
                "ordinal": ordinal,
            }
        result.append(normalized)
    return result


def _body_roles(
    contract_id: str, payload: Mapping[str, Any], privacy: str
) -> list[dict[str, Any]]:
    raw = payload.get("body_roles")
    if raw is None:
        body = payload.get("body", payload.get("plain_body", ""))
        raw = [
            {
                "role": "brief",
                "mode": "plain",
                "plain_body": "" if body is None else body,
                "interaction_contract_id": "human_value",
                "interaction_contract_version": 1,
            }
        ]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise ContractValidationError("body_roles must be a nonempty list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ContractValidationError("body role entries must be objects")
        role = _text(item.get("role", "brief"), "body role")
        if role in seen:
            raise ContractValidationError("body role is duplicated")
        seen.add(role)
        mode = str(item.get("mode", item.get("body_mode", "plain")))
        role_privacy = str(item.get("privacy_class", privacy))
        if role_privacy not in VALID_PRIVACY:
            raise ContractValidationError("body privacy class is invalid")
        contract_name = _text(
            item.get("interaction_contract_id", "human_value"),
            "interaction_contract_id",
        )
        version = item.get("interaction_contract_version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ContractValidationError("interaction contract version is invalid")
        if mode == "plain":
            if item.get("binding") is not None or item.get("document_binding") is not None:
                raise ContractValidationError("plain body cannot also have a document binding")
            plain = item.get("plain_body", item.get("text", ""))
            if not isinstance(plain, str):
                raise ContractValidationError("plain body must be text")
            binding = None
        elif mode == "document":
            if item.get("plain_body") is not None or item.get("text") is not None:
                raise ContractValidationError("document body cannot also contain plain text")
            value = item.get("binding", item.get("document_binding"))
            if not isinstance(value, Mapping):
                raise ContractValidationError("document body requires an explicit binding")
            binding_id = value.get("binding_id") or _stable_id(
                "contract-document-binding",
                [contract_id, role, value.get("store_id"), value.get("document_id")],
            )
            authority_epoch = value.get("authority_epoch", 1)
            if (
                isinstance(authority_epoch, bool)
                or not isinstance(authority_epoch, int)
                or authority_epoch <= 0
            ):
                raise ContractValidationError("document binding authority epoch is invalid")
            binding = {
                "binding_id": _text(binding_id, "binding_id"),
                "store_id": _text(value.get("store_id"), "binding.store_id"),
                "document_id": _text(value.get("document_id"), "binding.document_id"),
                "authority_epoch": authority_epoch,
            }
            if re.fullmatch(r"[0-9a-f]{32}", binding["binding_id"]) is None:
                raise ContractValidationError("document binding identity is invalid")
            plain = None
        else:
            raise ContractValidationError("body mode must be plain or document")
        body_revision = item.get("body_revision", 1)
        if (
            isinstance(body_revision, bool)
            or not isinstance(body_revision, int)
            or body_revision <= 0
        ):
            raise ContractValidationError("body revision is invalid")
        result.append(
            {
                "role": role,
                "mode": mode,
                "plain_body": plain,
                "binding": binding,
                "body_revision": body_revision,
                "interaction_contract_id": contract_name,
                "interaction_contract_version": version,
                "privacy_class": role_privacy,
            }
        )
    return sorted(result, key=lambda item: item["role"])


def _normalize_payload(
    payload: Mapping[str, Any],
    *,
    contract_id: str,
    revision: int,
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    title = _text(payload.get("title"), "title")
    status = str(payload.get("status", "draft")).strip().lower()
    if status not in VALID_STATUSES:
        raise ContractValidationError("contract status is invalid")
    privacy = str(payload.get("privacy_class", "private"))
    if privacy not in VALID_PRIVACY:
        raise ContractValidationError("privacy_class is invalid")
    progress = payload.get("estimated_progress", payload.get("progress", 0))
    if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
        raise ContractValidationError("estimated_progress must be between 0 and 100")
    lifecycle = str(payload.get("lifecycle", "current"))
    if lifecycle not in {"current", "archived", "tombstoned"}:
        raise ContractValidationError("contract lifecycle is invalid")
    tombstoned_at = payload.get("tombstoned_at")
    if lifecycle == "tombstoned" and tombstoned_at is None:
        tombstoned_at = updated_at
    if lifecycle != "tombstoned":
        tombstoned_at = None
    health = payload.get("health_inputs", {})
    if not isinstance(health, Mapping):
        raise ContractValidationError("health_inputs must be an object")
    aliases = _aliases(payload.get("aliases"))
    return {
        "schema": "wb.contract-snapshot/v1",
        "contract_id": contract_id,
        "title": title,
        "status": status,
        "type": _text(payload.get("type", payload.get("contract_type", "other")), "type"),
        "lifecycle": lifecycle,
        "privacy_class": privacy,
        "estimated_progress": progress,
        "current_revision": revision,
        "created_at": created_at,
        "updated_at": updated_at,
        "tombstoned_at": tombstoned_at,
        "aliases": aliases,
        "dates": _dates(payload),
        "commitments": _structured_list(
            contract_id, payload.get("commitments", []), kind="commitments"
        ),
        "constraints": _structured_list(
            contract_id, payload.get("constraints", []), kind="constraints"
        ),
        "health_inputs": {str(key): _json_safe(health[key]) for key in sorted(health)},
        "participants": _structured_list(
            contract_id, payload.get("participants", []), kind="participants"
        ),
        "evidence_links": _structured_list(
            contract_id, payload.get("evidence_links", []), kind="evidence_links"
        ),
        "body_roles": _body_roles(contract_id, payload, privacy),
    }


def _enforce_body_role_transitions(
    current: Mapping[str, Any], snapshot: dict[str, Any]
) -> None:
    """Keep role authority changes out of the generic entity patch path.

    A plain/document conversion needs a dedicated coordinator that can freeze
    the starting value and provision the shared document binding. Destructive
    document-to-plain conversion is not supported. Ordinary CAS updates may
    edit a plain value or its privacy and receive a monotonic body revision.
    """

    prior = {item["role"]: item for item in current["body_roles"]}
    proposed = {item["role"]: item for item in snapshot["body_roles"]}
    if not prior.keys() <= proposed.keys():
        raise ContractValidationError("existing body roles cannot be removed")
    for role, old in prior.items():
        new = proposed[role]
        if old["mode"] != new["mode"]:
            raise ContractValidationError(
                "body authority conversion requires a dedicated coordinator"
            )
        if (
            old["interaction_contract_id"] != new["interaction_contract_id"]
            or old["interaction_contract_version"]
            != new["interaction_contract_version"]
        ):
            raise ContractValidationError("body interaction contract is immutable")
        if old["mode"] == "document" and old["binding"] != new["binding"]:
            raise ContractValidationError(
                "document binding changes require a dedicated coordinator"
            )
        changed = (
            old["plain_body"] != new["plain_body"]
            or old["privacy_class"] != new["privacy_class"]
        )
        new["body_revision"] = int(old["body_revision"]) + (1 if changed else 0)


class ContractService:
    def __init__(self, store: ContractStore) -> None:
        self.store = store

    def authority(self) -> dict[str, Any]:
        return self.store.authority()

    def _resolve_id_locked(
        self, connection: sqlite3.Connection, reference: str
    ) -> str | None:
        value = str(reference).strip()
        row = connection.execute(
            "SELECT contract_id FROM contracts WHERE contract_id=?", (value,)
        ).fetchone()
        if row is not None:
            return str(row["contract_id"])
        try:
            alias = normalize_alias(value)
        except ContractValidationError:
            return None
        row = connection.execute(
            "SELECT contract_id FROM contract_aliases WHERE alias_key=?", (alias,)
        ).fetchone()
        return None if row is None else str(row["contract_id"])

    def _revision_snapshot_locked(
        self, connection: sqlite3.Connection, contract_id: str, revision: int
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT snapshot_json, snapshot_sha256 FROM contract_revisions "
            "WHERE contract_id=? AND revision=?",
            (contract_id, revision),
        ).fetchone()
        if row is None:
            raise ContractStoreError("contract revision is missing")
        snapshot = json.loads(str(row["snapshot_json"]))
        if _sha(snapshot) != row["snapshot_sha256"]:
            raise ContractStoreError("contract revision digest mismatch")
        return snapshot

    def _current_snapshot_locked(
        self, connection: sqlite3.Connection, contract_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT current_revision FROM contracts WHERE contract_id=?",
            (contract_id,),
        ).fetchone()
        if row is None:
            raise ContractNotFound("contract does not exist")
        return self._revision_snapshot_locked(
            connection, contract_id, int(row["current_revision"])
        )

    def get(self, reference: str, *, include_tombstoned: bool = False) -> dict[str, Any] | None:
        with self.store.read_transaction() as connection:
            contract_id = self._resolve_id_locked(connection, reference)
            if contract_id is None:
                return None
            snapshot = self._current_snapshot_locked(connection, contract_id)
            if snapshot["lifecycle"] == "tombstoned" and not include_tombstoned:
                return None
            return snapshot

    def list(
        self,
        *,
        status: str | None = None,
        include_tombstoned: bool = False,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in VALID_STATUSES:
            raise ContractValidationError("contract status is invalid")
        with self.store.read_transaction() as connection:
            clauses: list[str] = []
            values: list[Any] = []
            if status is not None:
                clauses.append("status=?")
                values.append(status)
            if not include_tombstoned:
                clauses.append("lifecycle!='tombstoned'")
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = connection.execute(
                "SELECT contract_id, current_revision FROM contracts"
                + where
                + " ORDER BY title COLLATE NOCASE, contract_id",
                values,
            ).fetchall()
            return [
                self._revision_snapshot_locked(
                    connection, str(row["contract_id"]), int(row["current_revision"])
                )
                for row in rows
            ]

    def _replay_locked(
        self, connection: sqlite3.Connection, intent_id: str, request_sha256: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM contract_mutation_receipts WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise IdempotencyConflict("intent_id was already used for another request")
        snapshot = self._revision_snapshot_locked(
            connection, str(row["contract_id"]), int(row["revision"])
        )
        if _sha(snapshot) != row["result_sha256"]:
            raise ContractStoreError("contract mutation receipt digest mismatch")
        return snapshot

    def _check_aliases_locked(
        self,
        connection: sqlite3.Connection,
        contract_id: str,
        aliases: Sequence[Mapping[str, Any]],
    ) -> None:
        for alias in aliases:
            row = connection.execute(
                "SELECT contract_id FROM contract_aliases WHERE alias_key=?",
                (alias["alias_key"],),
            ).fetchone()
            if row is not None and row["contract_id"] != contract_id:
                raise ContractConflict(
                    f"contract alias {alias['alias_display']!r} is already in use"
                )

    def _check_wip_locked(
        self,
        connection: sqlite3.Connection,
        snapshot: Mapping[str, Any],
        *,
        previous_status: str | None,
        previous_lifecycle: str | None = None,
    ) -> None:
        if snapshot["status"] != "active" or snapshot["lifecycle"] != "current":
            return
        if previous_status == "active" and previous_lifecycle == "current":
            return
        limit = int(
            connection.execute(
                "SELECT active_limit FROM contract_wip_policies WHERE policy_id='default'"
            ).fetchone()[0]
        )
        active_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM contracts "
                "WHERE status='active' AND lifecycle='current'"
            ).fetchone()[0]
        )
        if active_count >= limit:
            raise WipLimitExceeded(
                f"activating this contract would exceed the WIP limit of {limit}"
            )

    def _persist_current_locked(
        self,
        connection: sqlite3.Connection,
        snapshot: Mapping[str, Any],
        *,
        create: bool,
    ) -> None:
        contract_id = str(snapshot["contract_id"])
        values = (
            snapshot["title"],
            snapshot["status"],
            snapshot["type"],
            snapshot["lifecycle"],
            snapshot["privacy_class"],
            snapshot["estimated_progress"],
            snapshot["current_revision"],
            snapshot["created_at"],
            snapshot["updated_at"],
            snapshot["tombstoned_at"],
        )
        if create:
            connection.execute(
                "INSERT INTO contracts (contract_id,title,status,contract_type,lifecycle,"
                "privacy_class,estimated_progress,current_revision,created_at,updated_at,"
                "tombstoned_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (contract_id, *values),
            )
        else:
            connection.execute(
                "UPDATE contracts SET title=?,status=?,contract_type=?,lifecycle=?,"
                "privacy_class=?,estimated_progress=?,current_revision=?,created_at=?,"
                "updated_at=?,tombstoned_at=? WHERE contract_id=?",
                (*values, contract_id),
            )
        for alias in snapshot["aliases"]:
            connection.execute(
                "INSERT OR IGNORE INTO contract_aliases "
                "(alias_key,alias_display,alias_kind,contract_id,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    alias["alias_key"],
                    alias["alias_display"],
                    alias["alias_kind"],
                    contract_id,
                    snapshot["updated_at"],
                ),
            )
        for table in (
            "contract_dates",
            "contract_commitments",
            "contract_constraints",
            "contract_health_inputs",
            "contract_participants",
            "contract_evidence_links",
            "contract_body_roles",
        ):
            connection.execute(f"DELETE FROM {table} WHERE contract_id=?", (contract_id,))
        connection.execute(
            "UPDATE contract_document_bindings SET lifecycle='retired' "
            "WHERE contract_id=? AND lifecycle='current'",
            (contract_id,),
        )
        for kind, item in snapshot["dates"].items():
            connection.execute(
                "INSERT INTO contract_dates VALUES (?,?,?,?)",
                (contract_id, kind, item["value"], item["precision"]),
            )
        for item in snapshot["commitments"]:
            connection.execute(
                "INSERT INTO contract_commitments VALUES (?,?,?,?,?,?,?)",
                (
                    item["commitment_id"], contract_id, item["kind"], item["text"],
                    item["state"], item["due_date"], item["ordinal"],
                ),
            )
        for item in snapshot["constraints"]:
            connection.execute(
                "INSERT INTO contract_constraints VALUES (?,?,?,?,?,?)",
                (
                    item["constraint_id"], contract_id, item["kind"], item["text"],
                    item["state"], item["ordinal"],
                ),
            )
        for key, value in snapshot["health_inputs"].items():
            connection.execute(
                "INSERT INTO contract_health_inputs VALUES (?,?,?)",
                (contract_id, key, _canonical_json(value)),
            )
        for item in snapshot["participants"]:
            connection.execute(
                "INSERT INTO contract_participants VALUES (?,?,?,?,?,?)",
                (
                    item["participant_id"], contract_id, item["entity_ref"],
                    item["display_name"], item["role"], item["ordinal"],
                ),
            )
        for item in snapshot["evidence_links"]:
            connection.execute(
                "INSERT INTO contract_evidence_links VALUES (?,?,?,?,?,?,?)",
                (
                    item["evidence_link_id"], contract_id, item["evidence_ref"],
                    item["label"], item["requirement"], item["state"], item["ordinal"],
                ),
            )
        for item in snapshot["body_roles"]:
            binding_id = None
            if item["mode"] == "document":
                binding = item["binding"]
                binding_id = binding["binding_id"]
                existing = connection.execute(
                    "SELECT binding_id FROM contract_document_bindings WHERE binding_id=?",
                    (binding_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO contract_document_bindings "
                        "(binding_id,contract_id,body_role,store_id,document_id,"
                        "interaction_contract_id,interaction_contract_version,lifecycle,"
                        "authority_epoch,created_at) VALUES (?,?,?,?,?,?,?,'current',?,?)",
                        (
                            binding_id, contract_id, item["role"], binding["store_id"],
                            binding["document_id"], item["interaction_contract_id"],
                            item["interaction_contract_version"], binding["authority_epoch"],
                            snapshot["updated_at"],
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE contract_document_bindings SET lifecycle='current' "
                        "WHERE binding_id=?",
                        (binding_id,),
                    )
            connection.execute(
                "INSERT INTO contract_body_roles "
                "(contract_id,body_role,body_mode,plain_body,current_document_binding_id,"
                "body_revision,interaction_contract_id,interaction_contract_version,"
                "privacy_class) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    contract_id, item["role"], item["mode"], item["plain_body"], binding_id,
                    item["body_revision"], item["interaction_contract_id"],
                    item["interaction_contract_version"], item["privacy_class"],
                ),
            )

    def _record_mutation_locked(
        self,
        connection: sqlite3.Connection,
        snapshot: Mapping[str, Any],
        *,
        operation: str,
        request_sha256: str,
        actor_ref: str,
        intent_id: str,
        source_ref: str | None,
    ) -> None:
        snapshot_json = _canonical_json(snapshot)
        snapshot_sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        contract_id = str(snapshot["contract_id"])
        revision = int(snapshot["current_revision"])
        revision_id = _stable_id(
            "contract-revision", [contract_id, revision, request_sha256]
        )
        connection.execute(
            "INSERT INTO contract_revisions "
            "(revision_id,contract_id,revision,prior_revision,operation,snapshot_json,"
            "snapshot_sha256,"
            "request_sha256,actor_ref,intent_id,source_ref,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id, contract_id, revision,
                None if revision == 1 else revision - 1,
                operation, snapshot_json,
                snapshot_sha256, request_sha256, actor_ref, intent_id, source_ref,
                snapshot["updated_at"],
            ),
        )
        receipt_id = _stable_id("contract-mutation-receipt", [intent_id, request_sha256])
        connection.execute(
            "INSERT INTO contract_mutation_receipts "
            "(receipt_id,intent_id,operation,contract_id,revision,request_sha256,"
            "result_sha256,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                receipt_id, intent_id, operation, contract_id, revision, request_sha256,
                snapshot_sha256, snapshot["updated_at"],
            ),
        )
        search_payload = {
            "schema": "wb.contract-search-document/v1",
            "contract_id": contract_id,
            "revision": revision,
            "title": snapshot["title"],
            "status": snapshot["status"],
            "type": snapshot["type"],
            "privacy_class": snapshot["privacy_class"],
            "aliases": [item["alias_display"] for item in snapshot["aliases"]],
            "plain_bodies": [
                item["plain_body"]
                for item in snapshot["body_roles"]
                if item["mode"] == "plain"
            ],
        }
        content_sha256 = _sha(search_payload)
        event_kind = "delete" if snapshot["lifecycle"] == "tombstoned" else "upsert"
        event_id = _stable_id("contract-search-outbox", [contract_id, revision, event_kind])
        connection.execute(
            "INSERT INTO contract_search_outbox "
            "(event_id,contract_id,revision,event_kind,content_sha256,privacy_class,"
            "payload_json,committed_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                event_id, contract_id, revision, event_kind, content_sha256,
                snapshot["privacy_class"], _canonical_json(search_payload),
                snapshot["updated_at"],
            ),
        )

    def create(
        self,
        payload: Mapping[str, Any],
        *,
        actor: str,
        intent_id: str,
        source_ref: str | None = None,
        contract_id: str | None = None,
        operation: str = "create",
        enforce_wip: bool = True,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ContractValidationError("contract payload must be an object")
        if operation not in {"create", "legacy_import"}:
            raise ContractValidationError("contract create operation is invalid")
        if source_ref is not None and not isinstance(source_ref, str):
            raise ContractValidationError("source_ref must be text")
        actor_ref = _actor(actor)
        intent = _intent(intent_id)
        safe_payload = _json_safe(payload)
        request_sha256 = _sha(
            {
                "operation": operation,
                "payload": safe_payload,
                "source_ref": source_ref,
                "contract_id": contract_id,
                "enforce_wip": enforce_wip,
                "actor_ref": actor_ref,
            }
        )
        with self.store.write_transaction(connection) as tx:
            if operation != "legacy_import" or connection is None:
                require_mutations_open(tx, domain="contracts")
            replay = self._replay_locked(tx, intent, request_sha256)
            if replay is not None:
                return replay
            identifier = contract_id or uuid.uuid4().hex
            if not isinstance(identifier, str) or re.fullmatch(
                r"[0-9a-f]{32}", identifier
            ) is None:
                raise ContractValidationError(
                    "contract_id must be a 32-character opaque ID"
                )
            if tx.execute(
                "SELECT 1 FROM contracts WHERE contract_id=?", (identifier,)
            ).fetchone() is not None:
                raise ContractConflict("contract_id already exists")
            timestamp = _now()
            snapshot = _normalize_payload(
                safe_payload,
                contract_id=identifier,
                revision=1,
                created_at=timestamp,
                updated_at=timestamp,
            )
            if snapshot["lifecycle"] == "tombstoned":
                raise ContractValidationError("create cannot begin tombstoned")
            self._check_aliases_locked(tx, identifier, snapshot["aliases"])
            if enforce_wip:
                self._check_wip_locked(tx, snapshot, previous_status=None)
            self._persist_current_locked(tx, snapshot, create=True)
            self._record_mutation_locked(
                tx,
                snapshot,
                operation=operation,
                request_sha256=request_sha256,
                actor_ref=actor_ref,
                intent_id=intent,
                source_ref=source_ref,
            )
            return snapshot

    def update(
        self,
        reference: str,
        patch: Mapping[str, Any],
        *,
        expected_revision: int,
        actor: str,
        intent_id: str,
    ) -> dict[str, Any]:
        if not isinstance(patch, Mapping):
            raise ContractValidationError("contract patch must be an object")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise ContractValidationError("expected_revision must be a positive integer")
        actor_ref = _actor(actor)
        intent = _intent(intent_id)
        safe_patch = _json_safe(patch)
        with self.store.write_transaction() as tx:
            require_mutations_open(tx, domain="contracts")
            contract_id = self._resolve_id_locked(tx, reference)
            if contract_id is None:
                raise ContractNotFound("contract does not exist")
            request_sha256 = _sha(
                {
                    "operation": "update",
                    "contract_id": contract_id,
                    "expected_revision": expected_revision,
                    "patch": safe_patch,
                    "actor_ref": actor_ref,
                }
            )
            replay = self._replay_locked(tx, intent, request_sha256)
            if replay is not None:
                return replay
            current = self._current_snapshot_locked(tx, contract_id)
            if int(current["current_revision"]) != int(expected_revision):
                raise ContractConflict("contract revision changed")
            if current["lifecycle"] == "tombstoned":
                raise ContractConflict("tombstoned contracts cannot be updated")
            if safe_patch.get("lifecycle") == "tombstoned":
                raise ContractValidationError("use tombstone() for tombstone transitions")
            merged = dict(current)
            merged.update(safe_patch)
            if "aliases" in safe_patch:
                merged["aliases"] = list(current["aliases"]) + list(safe_patch["aliases"])
            timestamp = _now()
            snapshot = _normalize_payload(
                merged,
                contract_id=contract_id,
                revision=int(expected_revision) + 1,
                created_at=str(current["created_at"]),
                updated_at=timestamp,
            )
            _enforce_body_role_transitions(current, snapshot)
            self._check_aliases_locked(tx, contract_id, snapshot["aliases"])
            self._check_wip_locked(
                tx,
                snapshot,
                previous_status=str(current["status"]),
                previous_lifecycle=str(current["lifecycle"]),
            )
            self._persist_current_locked(tx, snapshot, create=False)
            self._record_mutation_locked(
                tx,
                snapshot,
                operation="update",
                request_sha256=request_sha256,
                actor_ref=actor_ref,
                intent_id=intent,
                source_ref=None,
            )
            return snapshot

    def tombstone(
        self,
        reference: str,
        *,
        expected_revision: int,
        actor: str,
        intent_id: str,
    ) -> dict[str, Any]:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise ContractValidationError("expected_revision must be a positive integer")
        actor_ref = _actor(actor)
        intent = _intent(intent_id)
        with self.store.write_transaction() as tx:
            require_mutations_open(tx, domain="contracts")
            contract_id = self._resolve_id_locked(tx, reference)
            if contract_id is None:
                raise ContractNotFound("contract does not exist")
            request_sha256 = _sha(
                {
                    "operation": "tombstone",
                    "contract_id": contract_id,
                    "expected_revision": expected_revision,
                    "actor_ref": actor_ref,
                }
            )
            replay = self._replay_locked(tx, intent, request_sha256)
            if replay is not None:
                return replay
            current = self._current_snapshot_locked(tx, contract_id)
            if int(current["current_revision"]) != int(expected_revision):
                raise ContractConflict("contract revision changed")
            if current["lifecycle"] == "tombstoned":
                raise ContractConflict("contract is already tombstoned")
            if any(item["mode"] == "document" for item in current["body_roles"]):
                raise ContractConflict(
                    "document-bound contract tombstone requires a dedicated coordinator"
                )
            merged = {**current, "lifecycle": "tombstoned"}
            timestamp = _now()
            snapshot = _normalize_payload(
                merged,
                contract_id=contract_id,
                revision=int(expected_revision) + 1,
                created_at=str(current["created_at"]),
                updated_at=timestamp,
            )
            self._persist_current_locked(tx, snapshot, create=False)
            self._record_mutation_locked(
                tx,
                snapshot,
                operation="tombstone",
                request_sha256=request_sha256,
                actor_ref=actor_ref,
                intent_id=intent,
                source_ref=None,
            )
            return snapshot

    def wip_status(self) -> dict[str, Any]:
        with self.store.read_transaction() as connection:
            policy = connection.execute(
                "SELECT active_limit FROM contract_wip_policies WHERE policy_id='default'"
            ).fetchone()
            rows = connection.execute(
                "SELECT title FROM contracts WHERE status='active' "
                "AND lifecycle='current' ORDER BY title COLLATE NOCASE, contract_id"
            ).fetchall()
            titles = [str(row["title"]) for row in rows]
            limit = int(policy["active_limit"])
            return {
                "within_limit": len(titles) <= limit,
                "active_count": len(titles),
                "limit": limit,
                "active_titles": titles,
            }

    def pending_search_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ContractValidationError("outbox limit must be a positive integer")
        with self.store.read_transaction() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM contract_search_outbox WHERE delivered_at IS NULL "
                    "ORDER BY committed_at,event_id LIMIT ?",
                    (limit,),
                )
            ]

    def mark_search_event_delivered(
        self, event_id: str, *, expected_content_sha256: str
    ) -> bool:
        identity = _text(event_id, "event_id")
        expected = _text(expected_content_sha256, "expected_content_sha256")
        if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise ContractValidationError("expected content digest must be SHA-256")
        with self.store.write_transaction() as connection:
            row = connection.execute(
                "SELECT content_sha256,delivered_at FROM contract_search_outbox "
                "WHERE event_id=?",
                (identity,),
            ).fetchone()
            if row is None:
                raise ContractNotFound("contract search event does not exist")
            if row["content_sha256"] != expected:
                raise ContractConflict("contract search event content changed")
            if row["delivered_at"] is not None:
                return False
            connection.execute(
                "UPDATE contract_search_outbox SET delivered_at=? WHERE event_id=?",
                (_now(), identity),
            )
            return True


# Imported late above to keep public exceptions grouped without a circular module import.
from work_buddy.contracts_domain.store import ContractStoreError  # noqa: E402


__all__ = [
    "ContractConflict",
    "ContractNotFound",
    "ContractService",
    "ContractValidationError",
    "IdempotencyConflict",
    "VALID_PRIVACY",
    "VALID_STATUSES",
    "WipLimitExceeded",
    "normalize_alias",
]
