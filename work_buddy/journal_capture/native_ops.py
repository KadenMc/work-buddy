"""Authority-aware Journal capability adapters.

The retained MCP capability names call these functions.  Database authority
uses only the Journal store; compatibility mode can still delegate to the
frozen Markdown implementation while Obsidian remains explicitly wanted.
"""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
from typing import Any, Iterable

from work_buddy.health.preferences import is_wanted
from work_buddy.journal_capture.authority import JournalAuthorityCoordinator
from work_buddy.journal_capture.domain import JournalDomainService
from work_buddy.journal_capture.models import JournalDayComposition, JournalFieldValue
from work_buddy.journal_capture.ingress import (
    agent_output_ingress_identity,
    commit_agent_output_source,
    commit_human_field_source,
    human_field_ingress_identity,
)
from work_buddy.journal_capture.native_source import JournalNativeSourceService
from work_buddy.journal_capture.projection import current_day
from work_buddy.journal_capture.store import JournalCaptureStore
from work_buddy.installed_authority import (
    InstalledAuthorityError,
    require_domain_store_open,
)


def _read_runtime():
    from work_buddy.journal_capture.api import _read_store_and_service

    return _read_store_and_service()


def _write_runtime():
    from work_buddy.journal_capture.api import _services

    return _services()


def _authority_mode(store: JournalCaptureStore) -> str:
    return JournalAuthorityCoordinator(store).state().mode


def _legacy_allowed() -> bool:
    return is_wanted("obsidian") is not False


def _guard_installed_journal() -> None:
    """Check the external seal before any runtime/dependency construction."""

    from work_buddy.paths import resolve

    require_domain_store_open("journal", resolve("db/journal-capture"))


def _native_state_is_uninitialized(exc: BaseException) -> bool:
    """Only a genuinely absent pre-cutover store may select compatibility."""

    from work_buddy.journal_capture.models import JournalCaptureError

    return (
        isinstance(exc, JournalCaptureError)
        and str(exc) == "journal_capture_state_not_initialized"
    )


def _resolve_local_date(target: str | None) -> str:
    today = date.fromisoformat(current_day()["localDate"])
    if target in {None, "", "today"}:
        return today.isoformat()
    if target == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    try:
        parsed = date.fromisoformat(target)
    except (TypeError, ValueError) as exc:
        raise ValueError("target must be today, yesterday, or YYYY-MM-DD") from exc
    if parsed.isoformat() != target:
        raise ValueError("target must be today, yesterday, or YYYY-MM-DD")
    return parsed.isoformat()


def _composition(
    domain: JournalDomainService,
    local_date: str,
    *,
    persist: bool,
    created_by: str = "work-buddy-journal-capability",
) -> JournalDayComposition:
    day = current_day(local_date)
    if persist:
        return domain.ensure_day(
            local_date=local_date,
            timezone=day["timezone"],
            boundary=day["dayBoundaryStart"],
            window_start=day["windowStart"],
            window_end=day["windowEnd"],
            boundary_policy_revision=None,
            created_by=created_by,
        )
    return domain.resolve_day(
        local_date=local_date,
        timezone=day["timezone"],
        boundary=day["dayBoundaryStart"],
        window_start=day["windowStart"],
        window_end=day["windowEnd"],
    )


def _field_view(item: JournalFieldValue) -> dict[str, Any]:
    return {
        "value_id": item.value_id,
        "module_instance_id": item.module_instance_id,
        "module_instance_version": item.module_instance_version,
        "field_id": item.field_id,
        "field_definition_version": item.field_definition_version,
        "value_kind": item.value_kind.value,
        "disposition": item.disposition.value if item.disposition is not None else None,
        "value": item.value,
        "revision": item.current_revision,
        "authorship": item.authorship,
        "review_state": item.review_state,
        "source_ref": item.source_ref,
        "observed_at": item.observed_at,
        "stated_at": item.stated_at,
    }


def _field_value_row(
    store: JournalCaptureStore,
    *,
    local_date: str,
    composition_slot_id: str,
) -> tuple[str, int] | None:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT value_id,current_revision FROM journal_field_values "
            "WHERE local_date=? AND composition_slot_id=? AND lifecycle='current' "
            "ORDER BY updated_at DESC,value_id",
            (local_date, composition_slot_id),
        ).fetchall()
    if len(rows) > 1:
        raise RuntimeError("That Journal field has more than one active value.")
    if not rows:
        return None
    return str(rows[0]["value_id"]), int(rows[0]["current_revision"])


def _module_for_field(composition: JournalDayComposition, module_slot_id: str):
    matches = [item.module for item in composition.modules if item.slot_id == module_slot_id]
    if len(matches) != 1:
        raise RuntimeError("The Journal field's module is unavailable.")
    return matches[0]


def _field_catalog(
    store: JournalCaptureStore,
    composition: JournalDayComposition,
) -> list[dict[str, Any]]:
    domain = JournalDomainService(store)
    result: list[dict[str, Any]] = []
    for field in composition.fields:
        module = _module_for_field(composition, field.module_slot_id)
        current_row = _field_value_row(
            store,
            local_date=composition.local_date,
            composition_slot_id=field.composition_slot_id,
        )
        current = domain.get_field_value(current_row[0]) if current_row is not None else None
        result.append(
            {
                "composition_slot_id": field.composition_slot_id,
                "module_slot_id": field.module_slot_id,
                "module_instance_id": module.module_instance_id,
                "module_instance_version": module.instance_version,
                "field_id": field.field_id,
                "field_definition_version": field.field_definition_version,
                "label": field.label,
                "description": field.description,
                "value_kind": field.value_kind,
                "unit": field.unit,
                "constraints": dict(field.constraints),
                "function_id": field.function_id,
                "function_version": field.function_version,
                "behavior_id": field.behavior_id,
                "behavior_version": field.behavior_version,
                "prompt": field.prompt_wording,
                "prompt_help": field.prompt_help,
                "requiredness": field.prompt_requiredness or "optional",
                "value": current.value if current is not None else None,
                "disposition": (
                    current.disposition.value
                    if current is not None and current.disposition is not None
                    else None
                ),
                "value_id": current.value_id if current is not None else None,
                "revision": current.current_revision if current is not None else 0,
                "authorship": current.authorship if current is not None else None,
                "review_state": current.review_state if current is not None else None,
                "source_ref": current.source_ref if current is not None else None,
            }
        )
    return result


def _sign_in_view(
    store: JournalCaptureStore,
    composition: JournalDayComposition,
) -> dict[str, Any]:
    fields = _field_catalog(store, composition)
    required = [item for item in fields if item["requiredness"] == "required"]
    complete = lambda item: item["value"] is not None or item["disposition"] is not None
    unique_ids = {
        item["field_id"]
        for item in fields
        if sum(candidate["field_id"] == item["field_id"] for candidate in fields) == 1
    }
    values = {
        item["field_id"]: item["value"]
        for item in fields
        if item["field_id"] in unique_ids
    }
    return {
        "profile": {
            "profile_id": composition.profile.profile_id,
            "profile_revision": composition.profile.profile_revision,
            "name": composition.profile.name,
            "composition_digest": composition.composition_digest,
        },
        "fields": fields,
        "values": values,
        "all_filled": all(complete(item) for item in required),
    }


def _native_journal_state(
    store: JournalCaptureStore,
    *,
    target: str | None,
    create_on_read: bool,
) -> dict[str, Any]:
    local_date = _resolve_local_date(target)
    domain = JournalDomainService(store)
    before = _day_exists(store, local_date)
    composition = _composition(domain, local_date, persist=create_on_read)
    items = domain.list_native_items(local_date)
    fields = domain.list_field_values(local_date)
    records = [
        item
        for item in items
        if item.item_kind in {"record", "log", "generated_artifact"}
    ]
    last_record = max(
        (item.updated_at for item in records),
        default=None,
    )
    now_day = current_day()
    collect_until = (
        now_day["now"]
        if local_date == now_day["localDate"]
        else composition.window_end
    )
    field_payload = [_field_view(item) for item in fields]
    return {
        "target_date": local_date,
        "ambiguous": False,
        "hint": "",
        "exists": before or composition.persisted or bool(items) or bool(fields),
        "created": create_on_read and not before and composition.persisted,
        "error": None,
        "collect_since": last_record or composition.window_start,
        "collect_until": collect_until,
        "timezone": composition.timezone,
        "day_boundary_start": composition.boundary,
        "window_start": composition.window_start,
        "window_end": composition.window_end,
        "last_log_ts": last_record,
        "log_section": "\n".join(
            item.plain_value or "" for item in records if item.plain_value
        ),
        "sign_in_section": json.dumps(
            {
                item["field_id"]: item["value"]
                for item in field_payload
                if item["disposition"] is None
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "authority_state": _authority_mode(store),
        "profile": {
            "profile_id": composition.profile.profile_id,
            "profile_revision": composition.profile.profile_revision,
            "name": composition.profile.name,
            "composition_digest": composition.composition_digest,
        },
        "items": [
            {
                "item_id": item.item_id,
                "module_instance_id": item.module_instance_id,
                "item_kind": item.item_kind,
                "value": item.plain_value,
                "revision": item.current_revision,
                "source_ref": item.source_ref,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in items
        ],
        "fields": field_payload,
    }


def journal_state(
    target: str | None = None,
    create_on_read: bool = False,
) -> dict[str, Any]:
    """Read native Journal state, preserving the legacy result keys."""

    _guard_installed_journal()
    try:
        store, _service = _read_runtime()
    except InstalledAuthorityError:
        raise
    except Exception as exc:
        if not _native_state_is_uninitialized(exc):
            raise
        if _legacy_allowed():
            from work_buddy.journal import read_journal_state

            return read_journal_state(target=target, create_on_read=create_on_read)
        return {
            "target_date": _resolve_local_date(target),
            "ambiguous": False,
            "hint": "",
            "exists": False,
            "created": False,
            "error": "Journal database authority is not initialized.",
            "collect_since": None,
            "collect_until": None,
            "last_log_ts": None,
            "log_section": None,
            "sign_in_section": None,
            "authority_state": "uninitialized",
        }
    mode = _authority_mode(store)
    if mode == "legacy_compatibility":
        if not _legacy_allowed():
            return {
                "target_date": _resolve_local_date(target),
                "ambiguous": False,
                "hint": "",
                "exists": False,
                "created": False,
                "error": "Journal database cutover is required; Obsidian is disabled.",
                "collect_since": None,
                "collect_until": None,
                "last_log_ts": None,
                "log_section": None,
                "sign_in_section": None,
                "authority_state": mode,
            }
        from work_buddy.journal import read_journal_state

        return read_journal_state(target=target, create_on_read=create_on_read)
    if create_on_read:
        _sources, writable, _capture = _write_runtime()
        JournalAuthorityCoordinator(writable).capture_mode()
        store = writable
    return _native_journal_state(
        store,
        target=target,
        create_on_read=create_on_read,
    )


def running_notes(
    *,
    same_day: bool = False,
    days: int | None = None,
    start: str | None = None,
    stop: str | None = None,
    journal_date: str | None = None,
) -> str:
    """Read native Running Notes without enumerating Journal Markdown."""

    _guard_installed_journal()
    if same_day:
        days = 1
    if days is not None and (start is not None or stop is not None):
        raise ValueError("Cannot combine 'days' with 'start'/'stop'")
    if days is not None and (isinstance(days, bool) or days < 1 or days > 3660):
        raise ValueError("days must be between 1 and 3660")

    try:
        store, _service = _read_runtime()
    except InstalledAuthorityError:
        raise
    except Exception as exc:
        if not _native_state_is_uninitialized(exc):
            raise
        if _legacy_allowed():
            from work_buddy.journal_backlog import read_running_notes

            return read_running_notes(
                same_day=same_day,
                days=days,
                start=start,
                stop=stop,
                journal_date=journal_date,
            )
        raise RuntimeError("Journal database authority is not initialized.")
    mode = _authority_mode(store)
    if mode == "legacy_compatibility":
        if not _legacy_allowed():
            raise RuntimeError("Journal database cutover is required; Obsidian is disabled.")
        from work_buddy.journal_backlog import read_running_notes

        return read_running_notes(
            same_day=same_day,
            days=days,
            start=start,
            stop=stop,
            journal_date=journal_date,
        )

    anchor = date.fromisoformat(_resolve_local_date(journal_date))
    if days is not None:
        selected = [anchor - timedelta(days=offset) for offset in range(days)]
    elif start is not None or stop is not None:
        lower = date.min if start is None else date.fromisoformat(start)
        upper = anchor if stop is None else date.fromisoformat(stop)
        if lower > upper:
            raise ValueError("start must be on or before stop")
        selected = list(_dates_descending(store, lower=lower, upper=upper))
    else:
        selected = [anchor]

    domain = JournalDomainService(store)
    sections: list[tuple[str, str]] = []
    for selected_date in selected:
        local_date = selected_date.isoformat()
        text = "\n".join(
            item.plain_value or ""
            for item in domain.list_native_items(local_date)
            if item.item_kind == "running_note" and item.plain_value
        )
        if text:
            sections.append((local_date, text))
    if len(sections) <= 1:
        return sections[0][1] if sections else ""
    return "\n\n".join(f"## {day}\n\n{text}" for day, text in sections)


def journal_sign_in(
    *,
    target: str | None = None,
    write_fields: str | dict[str, Any] | None = None,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    """Read or update the active profile's generic typed fields.

    Field keys may be a unique ``field_id`` or an exact
    ``composition_slot_id``. A value may be supplied directly or in an
    envelope containing ``value``, ``disposition``, and optionally
    ``expected_revision``/``stated_at``.
    """

    _guard_installed_journal()
    try:
        store, _service = _read_runtime()
    except InstalledAuthorityError:
        raise
    except Exception as exc:
        if not _native_state_is_uninitialized(exc):
            raise
        if _legacy_allowed():
            from work_buddy.mcp_server.context_wrappers import journal_sign_in as legacy

            return legacy(target=target, write_fields=write_fields)
        raise RuntimeError("Journal database authority is not initialized.")
    mode = _authority_mode(store)
    if mode == "legacy_compatibility":
        if not _legacy_allowed():
            raise RuntimeError("Journal database cutover is required; Obsidian is disabled.")
        from work_buddy.mcp_server.context_wrappers import journal_sign_in as legacy

        return legacy(target=target, write_fields=write_fields)

    local_date = _resolve_local_date(target)
    domain = JournalDomainService(store)
    composition = _composition(domain, local_date, persist=False)
    if write_fields is None:
        return {
            "target_date": local_date,
            "authority_state": mode,
            "sign_in": _sign_in_view(store, composition),
            "wellness": {
                "available": False,
                "reason": "No generic Journal function interpreter is registered.",
                "declared_functions": sorted(
                    {
                        field.function_id
                        for field in composition.fields
                        if field.function_id is not None
                    }
                ),
            },
        }

    parsed = json.loads(write_fields) if isinstance(write_fields, str) else write_fields
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("write_fields must be a non-empty JSON object")
    if not all(isinstance(key, str) and key for key in parsed):
        raise ValueError("write_fields keys must be field identities")

    sources, writable, _capture = _write_runtime()
    JournalAuthorityCoordinator(writable).capture_mode()
    store = writable
    domain = JournalDomainService(store)
    composition = _composition(domain, local_date, persist=False)

    from work_buddy.agent_session import get_originating_session
    from work_buddy.dashboard import local_identity_api

    session_id = get_originating_session()
    if not session_id:
        raise RuntimeError("Journal field writes require an attributed Work Buddy agent session.")
    enrolled_actor = local_identity_api._authority().enrolled_actor()
    batch_identity = client_mutation_id or (
        "journal-sign-in:"
        + hashlib.sha256(
            json.dumps(
                {
                    "target": local_date,
                    "write_fields": parsed,
                    "session": hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    if not isinstance(batch_identity, str) or not batch_identity.strip():
        raise ValueError("client_mutation_id must be non-empty")

    by_slot = {field.composition_slot_id: field for field in composition.fields}
    by_id: dict[str, list[Any]] = {}
    for field in composition.fields:
        by_id.setdefault(field.field_id, []).append(field)

    prepared: list[dict[str, Any]] = []
    for ordinal, (requested_key, submitted) in enumerate(parsed.items()):
        field = by_slot.get(requested_key)
        if field is None:
            matches = by_id.get(requested_key, [])
            if len(matches) != 1:
                if matches:
                    raise ValueError(
                        f"Field {requested_key!r} occurs more than once; use composition_slot_id."
                    )
                raise ValueError(f"Field {requested_key!r} is not in this Journal day.")
            field = matches[0]
        module = _module_for_field(composition, field.module_slot_id)

        envelope = (
            submitted
            if isinstance(submitted, dict)
            and any(
                key in submitted
                for key in ("value", "disposition", "expected_revision", "expectedRevision", "stated_at", "statedAt")
            )
            else {"value": submitted}
        )
        value = envelope.get("value")
        disposition = envelope.get("disposition")
        if disposition is not None and not isinstance(disposition, str):
            raise ValueError(f"Field {requested_key!r} disposition must be a string.")
        if "value" not in envelope and disposition is None:
            raise ValueError(f"Field {requested_key!r} needs a value or disposition.")
        stated_at = envelope.get("stated_at", envelope.get("statedAt"))
        if stated_at is not None and not isinstance(stated_at, str):
            raise ValueError(f"Field {requested_key!r} stated_at must be a string.")

        current = _field_value_row(
            store,
            local_date=local_date,
            composition_slot_id=field.composition_slot_id,
        )
        requested_revision = envelope.get(
            "expected_revision", envelope.get("expectedRevision", 0)
        )
        if isinstance(requested_revision, bool) or not isinstance(requested_revision, int):
            raise ValueError(f"Field {requested_key!r} expected_revision must be an integer.")

        semantic_value = (
            {"disposition": disposition}
            if disposition is not None and "value" not in envelope
            else {"value": value, "disposition": disposition}
        )
        exact_value = (
            value
            if isinstance(value, str) and disposition is None
            else json.dumps(
                semantic_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        semantic = {
            "schema": "wb.journal-human-field/v1",
            "batch": batch_identity,
            "ordinal": ordinal,
            "local_date": local_date,
            "composition_slot_id": field.composition_slot_id,
            "module": [module.module_instance_id, module.instance_version],
            "field": [field.field_id, field.field_definition_version],
            "expected_revision": requested_revision,
            "content_sha256": hashlib.sha256(exact_value.encode("utf-8")).hexdigest(),
        }
        identity = human_field_ingress_identity(
            enrolled_actor=enrolled_actor,
            session_id=session_id,
            semantic_request=semantic,
        )
        commit = commit_human_field_source(
            sources=sources,
            identity=identity,
            exact_value=exact_value,
            occurred_at=stated_at,
        )
        value_id = current[0] if current is not None else (
            "jfv_"
            + hashlib.sha256(
                json.dumps(
                    {
                        "schema": "wb.journal-field-value-id/v1",
                        "date": local_date,
                        "slot": field.composition_slot_id,
                        "batch": batch_identity,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:32]
        )
        prepared.append(
            {
                "field": field,
                "module": module,
                "value": value,
                "disposition": disposition,
                "stated_at": stated_at,
                "expected_revision": requested_revision,
                "value_id": value_id,
                "identity": identity,
                "commit": commit,
            }
        )

    composition = _composition(
        JournalDomainService(writable),
        local_date,
        persist=True,
        created_by="work-buddy-journal-human-field",
    )
    coordinator = JournalNativeSourceService(writable, sources)
    written: list[dict[str, Any]] = []
    session_sha = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    for item in prepared:
        field = item["field"]
        module = item["module"]
        identity = item["identity"]
        commit = item["commit"]
        result = coordinator.put_field_value(
            source_ref=commit.source_ref,
            representation_id=commit.representation_id,
            service_principal=identity.service_principal,
            value_id=item["value_id"],
            local_date=local_date,
            module_instance_id=module.module_instance_id,
            module_instance_version=module.instance_version,
            field_id=field.field_id,
            field_definition_version=field.field_definition_version,
            composition_slot_id=field.composition_slot_id,
            prompt_id=field.prompt_id,
            prompt_version=field.prompt_version,
            client_mutation_id=f"{identity.client_mutation_id}:field",
            expected_revision=item["expected_revision"],
            actor={
                "schema": "wb.journal-human-field-actor/v1",
                "actor": enrolled_actor.to_dict(),
                "relay_session_sha256": session_sha,
            },
            value=item["value"],
            disposition=item["disposition"],
            authorship="human",
            review_state="not_applicable",
            stated_at=item["stated_at"],
        )
        written.append(
            {
                "value_id": result.value_id,
                "field_id": result.field_id,
                "revision": result.current_revision,
                "source_ref": result.source_ref,
            }
        )

    return {
        "target_date": local_date,
        "authority_state": mode,
        "write_result": {
            "success": True,
            "fields_written": len(written),
            "fields": written,
            "composition_digest": composition.composition_digest,
        },
        "sign_in": _sign_in_view(writable, composition),
        "wellness": {
            "available": False,
            "reason": "No generic Journal function interpreter is registered.",
            "declared_functions": sorted(
                {
                    field.function_id
                    for field in composition.fields
                    if field.function_id is not None
                }
            ),
        },
    }


def day_planner(
    *,
    action: str = "status",
    target: str | None = None,
    calendar_events: str | list[Any] | None = None,
    focused_tasks: str | list[Any] | None = None,
    config_overrides: str | dict[str, Any] | None = None,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    """Generate and store native Journal day-plan artifacts."""

    _guard_installed_journal()
    try:
        store, _service = _read_runtime()
    except InstalledAuthorityError:
        raise
    except Exception as exc:
        if not _native_state_is_uninitialized(exc):
            raise
        if _legacy_allowed():
            from work_buddy.mcp_server.context_wrappers import day_planner as legacy

            return legacy(
                action=action,
                target=target,
                calendar_events=calendar_events,
                focused_tasks=focused_tasks,
                config_overrides=config_overrides,
            )
        raise RuntimeError("Journal database authority is not initialized.")
    mode = _authority_mode(store)
    if mode == "legacy_compatibility":
        if not _legacy_allowed():
            raise RuntimeError("Journal database cutover is required; Obsidian is disabled.")
        from work_buddy.mcp_server.context_wrappers import day_planner as legacy

        return legacy(
            action=action,
            target=target,
            calendar_events=calendar_events,
            focused_tasks=focused_tasks,
            config_overrides=config_overrides,
        )

    local_date = _resolve_local_date(target)
    domain = JournalDomainService(store)
    composition = _composition(domain, local_date, persist=False)
    if action == "status":
        included = [
            item for item in composition.modules if item.semantic_membership == "included"
        ]
        return {
            "ready": bool(included),
            "authority": "journal_sqlite",
            "provider": "native",
            "hasRemoteCalendars": False,
            "moduleCount": len(included),
            "reason": None if included else "The active Journal profile has no modules.",
        }
    if action == "read":
        for item in reversed(domain.list_native_items(local_date)):
            if item.item_kind != "generated_artifact" or not item.plain_value:
                continue
            try:
                payload = json.loads(item.plain_value)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("schema") != "wb.journal-day-plan/v1":
                continue
            entries = payload.get("entries")
            if not isinstance(entries, list):
                continue
            return {
                "entries": entries,
                "entry_count": len(entries),
                "item_id": item.item_id,
                "revision": item.current_revision,
                "target_date": local_date,
                "authority": "journal_sqlite",
            }
        return {
            "entries": [],
            "entry_count": 0,
            "item_id": None,
            "revision": 0,
            "target_date": local_date,
            "authority": "journal_sqlite",
        }

    def parse_list(value: str | list[Any] | None, name: str) -> list[Any]:
        parsed = json.loads(value) if isinstance(value, str) else (value or [])
        if not isinstance(parsed, list):
            raise ValueError(f"{name} must be a JSON list")
        return parsed

    def parse_config(value: str | dict[str, Any] | None) -> dict[str, Any]:
        parsed = json.loads(value) if isinstance(value, str) else (value or {})
        if not isinstance(parsed, dict):
            raise ValueError("config_overrides must be a JSON object")
        return parsed

    def persist(entries: list[Any]) -> dict[str, Any]:
        payload = json.dumps(
            {
                "schema": "wb.journal-day-plan/v1",
                "localDate": local_date,
                "entries": entries,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = journal_write(
            mode="briefing",
            target=local_date,
            briefing_md=payload,
            client_mutation_id=client_mutation_id,
        )
        return {
            "success": result.get("success") is True,
            "authority": "journal_sqlite",
            "target_date": local_date,
            "items": result.get("items", []),
        }

    if action in {"generate", "generate_and_write"}:
        from work_buddy.journal_capture.planner import generate_plan

        entries = generate_plan(
            parse_list(calendar_events, "calendar_events"),
            parse_list(focused_tasks, "focused_tasks"),
            parse_config(config_overrides),
        )
        result: dict[str, Any] = {
            "entries": entries,
            "entry_count": len(entries),
            "target_date": local_date,
        }
        if action == "generate_and_write":
            result["write_result"] = persist(entries)
        return result
    if action == "write":
        entries = parse_list(focused_tasks, "focused_tasks")
        return {"write_result": persist(entries), "entry_count": len(entries)}
    return {
        "error": (
            f"Unknown action: {action}. Use status/read/generate/write/generate_and_write."
        )
    }


def journal_write(
    *,
    mode: str = "log_entries",
    target: str | None = None,
    entries: str | list[Any] | None = None,
    briefing_md: str | None = None,
    client_mutation_id: str | None = None,
) -> dict[str, Any]:
    """Persist agent-authored records through Sources and native Journal."""

    _guard_installed_journal()
    sources, store, _capture = _write_runtime()
    authority_mode = _authority_mode(store)
    if authority_mode == "legacy_compatibility":
        if not _legacy_allowed():
            raise RuntimeError("Journal database cutover is required; Obsidian is disabled.")
        from work_buddy.mcp_server.context_wrappers import journal_write as legacy_write

        return legacy_write(
            mode=mode,
            target=target,
            entries=entries,
            briefing_md=briefing_md,
        )
    JournalAuthorityCoordinator(store).capture_mode()
    local_date = _resolve_local_date(target)

    if mode == "log_entries":
        parsed = json.loads(entries) if isinstance(entries, str) else entries
        if not isinstance(parsed, list) or not parsed:
            return {"error": "entries parameter required for log_entries mode"}
        values: list[tuple[str, str]] = []
        for raw in parsed:
            if (
                not isinstance(raw, (list, tuple))
                or len(raw) != 2
                or not all(isinstance(part, str) for part in raw)
                or not raw[1]
            ):
                raise ValueError("entries must be [time, description] string pairs")
            # Preserve the complete rendered record as the exact Source and
            # domain value. No Markdown tag or hidden file marker is added.
            values.append(("record", f"{raw[0]} - {raw[1]}"))
    elif mode == "briefing":
        if not isinstance(briefing_md, str) or not briefing_md:
            return {"error": "briefing_md parameter required for briefing mode"}
        values = [("generated_artifact", briefing_md)]
    else:
        return {"error": f"Unknown mode: {mode}. Use 'log_entries' or 'briefing'."}

    from work_buddy.agent_session import get_originating_session
    from work_buddy.dashboard import local_identity_api

    session_id = get_originating_session()
    if not session_id:
        raise RuntimeError("Journal writes require an attributed Work Buddy agent session.")
    enrolled_actor = local_identity_api._authority().enrolled_actor()
    domain = JournalDomainService(store)
    composition = _composition(domain, local_date, persist=False)
    batch_identity = client_mutation_id or (
        "journal-write:"
        + hashlib.sha256(
            json.dumps(
                {
                    "mode": mode,
                    "target": local_date,
                    "values": values,
                    "session": hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    if not isinstance(batch_identity, str) or not batch_identity.strip():
        raise ValueError("client_mutation_id must be non-empty")

    prepared = []
    for ordinal, (item_kind, plain_value) in enumerate(values):
        module = _module_for_item(composition, item_kind)
        semantic = {
            "schema": "wb.journal-agent-item/v1",
            "batch": batch_identity,
            "ordinal": ordinal,
            "local_date": local_date,
            "item_kind": item_kind,
            "module": [module.module_instance_id, module.instance_version],
            "content_sha256": hashlib.sha256(plain_value.encode("utf-8")).hexdigest(),
        }
        identity = agent_output_ingress_identity(
            enrolled_actor=enrolled_actor,
            session_id=session_id,
            operation="journal.write",
            semantic_request=semantic,
        )
        commit = commit_agent_output_source(
            sources,
            identity=identity,
            exact_text=plain_value,
            occurred_at=None,
        )
        prepared.append((item_kind, plain_value, module, identity, commit))

    # Freeze the day's effective profile only after every exact output is
    # durably retained. A retry reuses the same Sources and day snapshot.
    composition = _composition(
        domain,
        local_date,
        persist=True,
        created_by="work-buddy-journal-agent-output",
    )
    coordinator = JournalNativeSourceService(store, sources)
    created = []
    for ordinal, (item_kind, plain_value, module, identity, commit) in enumerate(prepared):
        item = coordinator.create_item(
            source_ref=commit.source_ref,
            representation_id=commit.representation_id,
            service_principal=identity.service_principal,
            local_date=local_date,
            item_kind=item_kind,
            plain_value=plain_value,
            interaction_behavior_id="provenance_only",
            interaction_behavior_version=1,
            client_mutation_id=f"{identity.client_mutation_id}:item",
            actor={
                "schema": "wb.journal-agent-actor/v1",
                "actor": identity.agent.to_dict(),
                "session_sha256": hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
            },
            module_instance_id=module.module_instance_id,
            module_instance_version=module.instance_version,
            authorship="ai",
            review_state="unreviewed",
        )
        created.append(
            {
                "item_id": item.item_id,
                "revision": item.current_revision,
                "source_ref": item.source_ref,
                "module_instance_id": item.module_instance_id,
            }
        )
    return {
        "success": True,
        "authority": "journal_sqlite",
        "target_date": local_date,
        "entries_written": len(created),
        "items": created,
        "composition_digest": composition.composition_digest,
    }


def _module_for_item(composition: JournalDayComposition, item_kind: str):
    included = [
        entry.module
        for entry in composition.modules
        if entry.semantic_membership == "included"
    ]
    if not included:
        raise RuntimeError("The active Journal profile has no module for this entry.")
    if item_kind == "running_note":
        preferred_ids = ("simple.notes",)
        preferred_types = ("running_notes", "record_collection")
    elif item_kind == "generated_artifact":
        preferred_ids = ()
        preferred_types = ("prompt_sequence", "reflection", "day_stream")
    else:
        preferred_ids = ("simple.stream",)
        preferred_types = ("day_stream", "record_collection")
    for module_id in preferred_ids:
        match = next(
            (module for module in included if module.module_instance_id == module_id),
            None,
        )
        if match is not None:
            return match
    for module_type in preferred_types:
        match = next(
            (module for module in included if module.module_type_id == module_type),
            None,
        )
        if match is not None:
            return match
    return included[0]


def _day_exists(store: JournalCaptureStore, local_date: str) -> bool:
    with store._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM journal_days AS day WHERE local_date=? AND ("
            "day.import_cohort_id IS NULL OR ("
            "EXISTS(SELECT 1 FROM journal_import_cohorts AS cohort "
            "WHERE cohort.cohort_id=day.import_cohort_id AND cohort.state='sealed') "
            "AND EXISTS(SELECT 1 FROM journal_authority_control AS authority "
            "WHERE authority.singleton=1 AND authority.mode='database_only')))",
            (local_date,),
        ).fetchone()
    return row is not None


def _dates_descending(
    store: JournalCaptureStore,
    *,
    lower: date,
    upper: date,
) -> Iterable[date]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT day.local_date FROM journal_days AS day "
            "WHERE day.local_date BETWEEN ? AND ? AND ("
            "day.import_cohort_id IS NULL OR ("
            "EXISTS(SELECT 1 FROM journal_import_cohorts AS cohort "
            "WHERE cohort.cohort_id=day.import_cohort_id AND cohort.state='sealed') "
            "AND EXISTS(SELECT 1 FROM journal_authority_control AS authority "
            "WHERE authority.singleton=1 AND authority.mode='database_only'))) "
            "UNION SELECT item.local_date FROM journal_items AS item "
            "WHERE item.local_date BETWEEN ? AND ? AND ("
            "item.import_cohort_id IS NULL OR ("
            "EXISTS(SELECT 1 FROM journal_import_cohorts AS cohort "
            "WHERE cohort.cohort_id=item.import_cohort_id AND cohort.state='sealed') "
            "AND EXISTS(SELECT 1 FROM journal_authority_control AS authority "
            "WHERE authority.singleton=1 AND authority.mode='database_only'))) "
            "ORDER BY local_date DESC",
            (lower.isoformat(), upper.isoformat(), lower.isoformat(), upper.isoformat()),
        ).fetchall()
    return tuple(date.fromisoformat(str(row[0])) for row in rows)


__all__ = [
    "day_planner",
    "journal_sign_in",
    "journal_state",
    "journal_write",
    "running_notes",
]
