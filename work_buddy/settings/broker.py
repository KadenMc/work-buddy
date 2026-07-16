"""Authoritative Settings broker for definitions, values, and transitions."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from work_buddy import config as wb_config
from work_buddy.journal_day import (
    DEFAULT_DAY_BOUNDARY,
    InvalidLocalTime,
    JournalDayWindow,
    day_for_instant,
    next_safe_boundary_transition,
    parse_local_time,
    window_for_local_date,
)
from work_buddy.settings import registry
from work_buddy.settings import store


logger = logging.getLogger(__name__)
_logged_timezone_drifts: set[tuple[str, str]] = set()


class SettingsError(Exception):
    """A stable HTTP-facing settings failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        value: dict[str, Any] | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.value = value
        self.field = field

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.value is not None:
            payload["value"] = self.value
        if self.field is not None:
            payload["field"] = self.field
        return payload


def _observed_at(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return result


def _timezone_name() -> str:
    zone = wb_config.USER_TZ
    return getattr(zone, "key", str(zone))


def _timezone_diagnostics(active_timezone: str) -> list[dict[str, Any]]:
    configured_timezone = _timezone_name()
    if configured_timezone == active_timezone:
        return []
    key = (active_timezone, configured_timezone)
    if key not in _logged_timezone_drifts:
        _logged_timezone_drifts.add(key)
        logger.warning(
            "timezone_config_drift: active Journal policy timezone %s differs "
            "from configured timezone %s; no days were reinterpreted",
            active_timezone,
            configured_timezone,
        )
    return [
        {
            "code": "timezone_config_drift",
            "active_timezone": active_timezone,
            "configured_timezone": configured_timezone,
            "message": (
                "The configured Work Buddy timezone differs from the Journal "
                "policy timezone. Existing and current Journal days remain on "
                "the persisted policy until a formal timezone migration is applied."
            ),
        }
    ]


def _configured_default(setting_id: str) -> Any:
    definition = registry.definition_for(setting_id)
    if definition is None:
        raise SettingsError(
            "unknown_setting",
            f"Unknown setting: {setting_id}",
            status_code=404,
        )
    value = definition["default_value"]
    if setting_id == registry.JOURNAL_DAY_BOUNDARY_ID:
        candidate = (
            (wb_config.load_config().get("journal") or {}).get("day_boundary")
            or value
        )
        try:
            parse_local_time(candidate)
        except InvalidLocalTime:
            candidate = DEFAULT_DAY_BOUNDARY
        value = candidate
    return value


def _now_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _json_value(raw: str | None) -> Any | None:
    return None if raw is None else json.loads(raw)


def _row_effective_value(row) -> str:
    active_override = _json_value(row["active_value_json"])
    return _row_default_value(row) if active_override is None else active_override


def _row_default_value(row) -> str:
    stored = _json_value(row["bootstrap_default_value_json"])
    # Additive migration fallback: _ensure_row persists this immediately.
    return _configured_default(row["setting_id"]) if stored is None else stored


def _insert_policy_base(
    conn,
    *,
    boundary: str,
    timezone_name: str,
    setting_revision: int,
    created_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO journal_day_policy_epoch (
            effective_local_date, window_start, boundary, timezone,
            setting_revision, created_at
        ) VALUES (NULL, NULL, ?, ?, ?, ?)
        """,
        (
            boundary,
            timezone_name,
            setting_revision,
            _now_iso(created_at),
        ),
    )


def _insert_policy_transition(
    conn,
    *,
    boundary: str,
    timezone_name: str,
    effective_at: datetime,
    setting_revision: int,
    created_at: datetime,
) -> None:
    zone = ZoneInfo(timezone_name)
    effective_local_date = effective_at.astimezone(zone).date().isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO journal_day_policy_epoch (
            effective_local_date, window_start, boundary, timezone,
            setting_revision, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            effective_local_date,
            _now_iso(effective_at),
            boundary,
            timezone_name,
            setting_revision,
            _now_iso(created_at),
        ),
    )


def _ensure_policy_history(conn, row, observed_at: datetime) -> None:
    """Create a durable reconstruction anchor for Journal-day windows.

    Existing installations may already have one applied transition recorded on
    ``setting_value_state``.  Preserve that last known transition when adding
    the history table so an upgrade does not immediately reinterpret its two
    adjacent Journal days.  Older transitions pre-dating both stores remain a
    documented legacy fallback to the base policy.
    """
    exists = conn.execute(
        "SELECT 1 FROM journal_day_policy_epoch LIMIT 1"
    ).fetchone()
    if exists is not None:
        return

    timezone_name = row["active_timezone"] or _timezone_name()
    if row["applied_at"] and row["applied_from_value_json"] is not None:
        _insert_policy_base(
            conn,
            boundary=_json_value(row["applied_from_value_json"]),
            timezone_name=timezone_name,
            setting_revision=max(0, int(row["revision"]) - 1),
            created_at=observed_at,
        )
        _insert_policy_transition(
            conn,
            boundary=_row_effective_value(row),
            timezone_name=timezone_name,
            effective_at=datetime.fromisoformat(row["applied_at"]),
            setting_revision=int(row["revision"]),
            created_at=observed_at,
        )
        return


    _insert_policy_base(
        conn,
        boundary=_row_effective_value(row),
        timezone_name=timezone_name,
        setting_revision=int(row["revision"]),
        created_at=observed_at,
    )


def _ensure_row(conn, setting_id: str, observed_at: datetime):
    bootstrap_default = _configured_default(setting_id)
    conn.execute(
        """
        INSERT OR IGNORE INTO setting_value_state (
            setting_id, scope, scope_id, bootstrap_default_value_json,
            bootstrap_source, value_version, active_timezone, active_value_json,
            pending_value_json, pending_source, pending_timezone, effective_at,
            revision, updated_at
        ) VALUES (?, 'profile', ?, ?, 'config-bootstrap', 1,
                  ?, NULL, NULL, NULL, NULL, NULL, 0, ?)
        """,
        (
            setting_id,
            registry.PROFILE_SCOPE_ID,
            json.dumps(bootstrap_default),
            _timezone_name(),
            _now_iso(observed_at),
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM setting_value_state
        WHERE setting_id = ? AND scope = 'profile' AND scope_id = ?
        """,
        (setting_id, registry.PROFILE_SCOPE_ID),
    ).fetchone()
    if row["bootstrap_default_value_json"] is None:
        # One-time upgrade of an early proving-slice database. Config is read
        # here exactly once; after this write the broker/store is authoritative.
        conn.execute(
            """
            UPDATE setting_value_state
            SET bootstrap_default_value_json = ?,
                bootstrap_source = 'config-migration', value_version = 1,
                updated_at = ?
            WHERE setting_id = ? AND scope = 'profile' AND scope_id = ?
            """,
            (
                json.dumps(bootstrap_default),
                _now_iso(observed_at),
                setting_id,
                registry.PROFILE_SCOPE_ID,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM setting_value_state
            WHERE setting_id = ? AND scope = 'profile' AND scope_id = ?
            """,
            (setting_id, registry.PROFILE_SCOPE_ID),
        ).fetchone()
    if setting_id == registry.JOURNAL_DAY_BOUNDARY_ID:
        _ensure_policy_history(conn, row, observed_at)
        if row["active_timezone"] is None:
            latest_epoch = conn.execute(
                """
                SELECT timezone FROM journal_day_policy_epoch
                ORDER BY (effective_local_date IS NULL) ASC,
                         effective_local_date DESC, sequence DESC
                LIMIT 1
                """
            ).fetchone()
            conn.execute(
                """
                UPDATE setting_value_state SET active_timezone = ?, updated_at = ?
                WHERE setting_id = ? AND scope = 'profile' AND scope_id = ?
                """,
                (
                    latest_epoch["timezone"] if latest_epoch else _timezone_name(),
                    _now_iso(observed_at),
                    setting_id,
                    registry.PROFILE_SCOPE_ID,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM setting_value_state
                WHERE setting_id = ? AND scope = 'profile' AND scope_id = ?
                """,
                (setting_id, registry.PROFILE_SCOPE_ID),
            ).fetchone()
    return row


def _change_event(record: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "type": "settings.changed",
        "setting_ids": [record["setting_id"]],
        "scope": record["scope"]["kind"],
        "registry_revision": registry.REGISTRY_REVISION,
        "value_revision": record["revision"],
        "affected_contexts": [
            "app:wb.journal",
            "subsystem:wb.journal/day-lifecycle",
            "view:wb.journal.main",
        ],
        "apply_status": record["apply_status"],
        "reason": reason,
    }


def publish_change(event: dict[str, Any] | None) -> None:
    """Best-effort publish through the dashboard's existing SSE event bus."""
    if event is None:
        return
    try:
        from work_buddy.dashboard.events import publish_auto

        publish_auto("settings.changed", event)
    except Exception:
        # Settings persistence is authoritative; transient UI invalidation is
        # recoverable by the next snapshot and must not roll a write back.
        return


def _promote_if_due(conn, row, observed_at: datetime):
    effective_at_raw = row["effective_at"]
    if row["pending_source"] is None or effective_at_raw is None:
        return row, False
    effective_at = datetime.fromisoformat(effective_at_raw)
    if observed_at.astimezone(timezone.utc) < effective_at.astimezone(timezone.utc):
        return row, False

    old_active_override = _json_value(row["active_value_json"])
    old_effective_value = (
        _row_default_value(row) if old_active_override is None else old_active_override
    )
    active_value = (
        row["pending_value_json"] if row["pending_source"] == "profile" else None
    )
    conn.execute(
        """
        UPDATE setting_value_state
        SET active_value_json = ?,
            active_timezone = COALESCE(pending_timezone, active_timezone),
            pending_value_json = NULL,
            pending_source = NULL, pending_timezone = NULL, effective_at = NULL,
            applied_from_value_json = ?, applied_at = ?,
            revision = revision + 1, updated_at = ?
        WHERE setting_id = ? AND scope = 'profile' AND scope_id = ?
        """,
        (
            active_value,
            json.dumps(old_effective_value),
            effective_at_raw,
            _now_iso(observed_at),
            row["setting_id"],
            registry.PROFILE_SCOPE_ID,
        ),
    )
    if row["setting_id"] == registry.JOURNAL_DAY_BOUNDARY_ID:
        new_effective_value = (
            _row_default_value(row)
            if row["pending_source"] == "default"
            else _json_value(row["pending_value_json"])
        )
        _insert_policy_transition(
            conn,
            boundary=new_effective_value,
            timezone_name=(
                row["pending_timezone"]
                or row["active_timezone"]
                or _timezone_name()
            ),
            effective_at=effective_at,
            setting_revision=int(row["revision"]) + 1,
            created_at=observed_at,
        )
    promoted = _ensure_row(conn, row["setting_id"], observed_at)
    return promoted, True


def _impact_preview(
    *,
    observed_at: datetime,
    effective_value: str,
    pending_value: str | None,
    pending_effective_at: datetime | None,
    pending_timezone_name: str | None,
    last_transition_at: datetime | None,
    active_timezone_name: str,
) -> dict[str, Any]:
    zone = ZoneInfo(active_timezone_name)
    current_date = day_for_instant(observed_at, zone, effective_value)
    current_window = window_for_local_date(current_date, zone, effective_value)
    if pending_value is not None and pending_effective_at is not None:
        pending_zone = ZoneInfo(pending_timezone_name or _timezone_name())
        transition_day = pending_effective_at.astimezone(pending_zone).date()
        if (
            observed_at.astimezone(timezone.utc)
            < pending_effective_at.astimezone(timezone.utc)
            and current_date >= transition_day
        ):
            current_date = transition_day - timedelta(days=1)
            recurring = window_for_local_date(current_date, zone, effective_value)
            current_window = JournalDayWindow(
                local_date=current_date,
                timezone=getattr(zone, "key", str(zone)),
                boundary=effective_value,
                start=recurring.start,
                end=pending_effective_at.astimezone(zone),
            )
        elif current_date == transition_day - timedelta(days=1):
            current_window = JournalDayWindow(
                local_date=current_date,
                timezone=getattr(zone, "key", str(zone)),
                boundary=effective_value,
                start=current_window.start,
                end=pending_effective_at.astimezone(zone),
            )
    elif last_transition_at is not None:
        transition_day = last_transition_at.astimezone(zone).date()
        if current_date == transition_day and (
            last_transition_at.astimezone(timezone.utc)
            > current_window.start.astimezone(timezone.utc)
        ):
            current_window = JournalDayWindow(
                local_date=current_date,
                timezone=getattr(zone, "key", str(zone)),
                boundary=effective_value,
                start=last_transition_at.astimezone(zone),
                end=current_window.end,
            )
    payload: dict[str, Any] = {
        "timezone": active_timezone_name,
        "current_day": current_window.as_dict(),
        "pending_day": None,
    }
    if pending_value is not None and pending_effective_at is not None:
        pending_zone = ZoneInfo(pending_timezone_name or _timezone_name())
        transition = pending_effective_at.astimezone(pending_zone)
        pending_date = transition.date()
        recurring = window_for_local_date(pending_date, pending_zone, pending_value)
        # A boundary change starts the first new logical day at the actual
        # safe transition instant.  For an earlier new boundary this is later
        # than the recurring wall time; for a later one they coincide.
        payload["pending_day"] = JournalDayWindow(
            local_date=pending_date,
            timezone=getattr(pending_zone, "key", str(pending_zone)),
            boundary=pending_value,
            start=transition,
            end=recurring.end,
        ).as_dict()
    return payload


def _record_from_row(row, observed_at: datetime) -> dict[str, Any]:
    default = _row_default_value(row)
    active_override = _json_value(row["active_value_json"])
    effective_value = default if active_override is None else active_override
    effective_source = "default" if active_override is None else "profile"

    has_pending = row["pending_source"] is not None
    pending_value = _json_value(row["pending_value_json"]) if has_pending else None
    if has_pending and pending_value is None:
        pending_value = default
    configured_value = pending_value if has_pending else effective_value
    configured_source = row["pending_source"] if has_pending else effective_source

    active_timezone_name = row["active_timezone"] or _timezone_name()
    pending_timezone_name = (
        row["pending_timezone"] or active_timezone_name if has_pending else None
    )
    effective_at = None
    effective_at_instant = None
    if row["effective_at"]:
        effective_at_instant = datetime.fromisoformat(row["effective_at"])
        effective_at = effective_at_instant.astimezone(
            ZoneInfo(pending_timezone_name or _timezone_name())
        ).isoformat()

    last_transition = None
    if row["applied_at"] and row["applied_from_value_json"] is not None:
        last_transition = {
            "from_value": _json_value(row["applied_from_value_json"]),
            "applied_at": (
                datetime.fromisoformat(row["applied_at"])
                .astimezone(ZoneInfo(active_timezone_name))
                .isoformat()
            ),
        }

    return {
        "setting_id": row["setting_id"],
        "value_version": int(row["value_version"]),
        "policy_timezone": active_timezone_name,
        "configured_timezone": _timezone_name(),
        "diagnostics": _timezone_diagnostics(active_timezone_name),
        "scope": {"kind": "profile", "subject_id": registry.PROFILE_SCOPE_ID},
        "default_value": default,
        "default_source": row["bootstrap_source"],
        "effective_value": effective_value,
        "configured_value": configured_value,
        "source": effective_source,
        "configured_source": configured_source,
        "is_modified": configured_source == "profile",
        "revision": f"value:{row['revision']}",
        "pending_value": pending_value if has_pending else None,
        "pending_timezone": pending_timezone_name,
        "effective_at": effective_at,
        "last_transition": last_transition,
        "apply_status": "pending" if has_pending else "effective",
        "impact_preview": _impact_preview(
            observed_at=observed_at,
            effective_value=effective_value,
            pending_value=pending_value if has_pending else None,
            pending_effective_at=effective_at_instant,
            pending_timezone_name=pending_timezone_name,
            last_transition_at=(
                datetime.fromisoformat(row["applied_at"])
                if row["applied_at"]
                else None
            ),
            active_timezone_name=active_timezone_name,
        ),
    }


def _read_value(setting_id: str, observed_at: datetime):
    if registry.definition_for(setting_id) is None:
        raise SettingsError(
            "unknown_setting",
            f"Unknown setting: {setting_id}",
            status_code=404,
        )
    conn = store.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_row(conn, setting_id, observed_at)
        row, promoted = _promote_if_due(conn, row, observed_at)
        record = _record_from_row(row, observed_at)
        conn.commit()
    finally:
        conn.close()
    event = _change_event(record, "pending-value-applied") if promoted else None
    publish_change(event)
    return record, event


def get_registry() -> dict[str, Any]:
    return registry.registry_payload()


def get_values(
    *,
    context_id: str | None = None,
    observed_at: datetime | None = None,
    read_only: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now = _observed_at(observed_at)
    try:
        setting_ids = registry.setting_ids_for_context(context_id)
    except KeyError as exc:
        raise SettingsError(
            "unknown_context",
            f"Unknown settings context: {context_id}",
            status_code=404,
        ) from exc

    values: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for setting_id in setting_ids:
        value, event = _read_value(setting_id, now)
        values.append(value)
        if event is not None:
            events.append(event)
    return (
        {
            "schema_version": registry.SCHEMA_VERSION,
            "registry_revision": registry.REGISTRY_REVISION,
            "timezone": (
                values[0]["policy_timezone"] if values else _timezone_name()
            ),
            "configured_timezone": _timezone_name(),
            "diagnostics": [
                diagnostic
                for value in values
                for diagnostic in value.get("diagnostics", [])
            ],
            "observed_at": now.astimezone(timezone.utc).isoformat(),
            "read_only": bool(read_only),
            "values": values,
        },
        events,
    )


def _validate_request(
    setting_id: str,
    scope: str,
    value: Any | None = None,
    *,
    validate_value: bool = False,
) -> None:
    definition = registry.definition_for(setting_id)
    if definition is None:
        raise SettingsError(
            "unknown_setting",
            f"Unknown setting: {setting_id}",
            status_code=404,
        )
    if scope not in definition["allowed_scopes"]:
        raise SettingsError(
            "validation_error",
            f"Scope {scope!r} is not allowed for {setting_id}",
            status_code=400,
            field="scope",
        )
    if validate_value and setting_id == registry.JOURNAL_DAY_BOUNDARY_ID:
        try:
            parse_local_time(value)
        except (InvalidLocalTime, TypeError) as exc:
            raise SettingsError(
                "validation_error",
                str(exc),
                status_code=400,
                field="value",
            ) from exc


def _assert_revision(
    expected_revision: str | None,
    record: dict[str, Any],
) -> None:
    if not isinstance(expected_revision, str) or not expected_revision:
        raise SettingsError(
            "validation_error",
            "expected_revision is required",
            status_code=400,
            field="expected_revision",
        )
    if expected_revision != record["revision"]:
        raise SettingsError(
            "revision_conflict",
            "The setting changed after this page loaded. Reconcile the authoritative value.",
            status_code=409,
            value=record,
        )


def _write_pending(
    *,
    setting_id: str,
    target_value: str,
    target_source: str,
    expected_revision: str | None,
    observed_at: datetime,
    read_only: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if read_only:
        raise SettingsError(
            "read_only",
            "Dashboard settings are read-only.",
            status_code=403,
        )

    conn = store.get_connection()
    promotion_event: dict[str, Any] | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_row(conn, setting_id, observed_at)
        row, promoted = _promote_if_due(conn, row, observed_at)
        current = _record_from_row(row, observed_at)
        if promoted:
            promotion_event = _change_event(current, "pending-value-applied")
        try:
            _assert_revision(expected_revision, current)
        except SettingsError:
            conn.commit()
            if promotion_event is not None:
                publish_change(promotion_event)
            raise

        # Choosing the already-effective value is a safe cancellation of any
        # pending transition.  A reset may additionally remove an active
        # profile override immediately when doing so leaves the effective
        # value unchanged.
        if target_value == current["effective_value"]:
            removes_redundant_override = (
                target_source == "default" and current["source"] == "profile"
            )
            if current["pending_value"] is None and not removes_redundant_override:
                conn.commit()
                if promotion_event is not None:
                    publish_change(promotion_event)
                return current, None
            conn.execute(
                """
                UPDATE setting_value_state
                SET active_value_json = CASE WHEN ? THEN NULL ELSE active_value_json END,
                    pending_value_json = NULL, pending_source = NULL,
                    pending_timezone = NULL, effective_at = NULL,
                    revision = revision + 1, updated_at = ?
                WHERE setting_id = ? AND scope = 'profile' AND scope_id = ?
                """,
                (
                    1 if removes_redundant_override else 0,
                    _now_iso(observed_at),
                    setting_id,
                    registry.PROFILE_SCOPE_ID,
                ),
            )
            row = _ensure_row(conn, setting_id, observed_at)
            record = _record_from_row(row, observed_at)
            conn.commit()
            if promotion_event is not None:
                publish_change(promotion_event)
            return record, _change_event(record, "pending-value-cancelled")

        if (
            current["pending_value"] == target_value
            and current["configured_source"] == target_source
        ):
            conn.commit()
            if promotion_event is not None:
                publish_change(promotion_event)
            return current, None

        transition = next_safe_boundary_transition(
            observed_at,
            ZoneInfo(current["policy_timezone"]),
            current["effective_value"],
            target_value,
        )
        conn.execute(
            """
            UPDATE setting_value_state
            SET pending_value_json = ?, pending_source = ?, pending_timezone = ?,
                effective_at = ?,
                revision = revision + 1, updated_at = ?
            WHERE setting_id = ? AND scope = 'profile' AND scope_id = ?
            """,
            (
                json.dumps(target_value),
                target_source,
                current["policy_timezone"],
                _now_iso(transition),
                _now_iso(observed_at),
                setting_id,
                registry.PROFILE_SCOPE_ID,
            ),
        )
        row = _ensure_row(conn, setting_id, observed_at)
        record = _record_from_row(row, observed_at)
        conn.commit()
    finally:
        conn.close()
    if promotion_event is not None:
        publish_change(promotion_event)
    return record, _change_event(record, "pending-value-scheduled")


def update_value(
    setting_id: str,
    *,
    scope: str,
    value: Any,
    expected_revision: str | None,
    observed_at: datetime | None = None,
    read_only: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    _validate_request(setting_id, scope, value, validate_value=True)
    return _write_pending(
        setting_id=setting_id,
        target_value=value,
        target_source="profile",
        expected_revision=expected_revision,
        observed_at=_observed_at(observed_at),
        read_only=read_only,
    )


def _preview_record(setting_id: str, observed_at: datetime) -> dict[str, Any]:
    """Read preview state without persisting the proposed value.

    A due transition is independent canonical maintenance, not part of the
    proposal. If preview is the first request after ``effective_at``, commit and
    publish that promotion exactly as a normal values read would; all other
    preview-only work is rolled back.
    """
    due_promotion = False
    conn = store.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_row(conn, setting_id, observed_at)
        row, due_promotion = _promote_if_due(conn, row, observed_at)
        record = _record_from_row(row, observed_at)
        conn.rollback()
    finally:
        conn.close()
    if due_promotion:
        record, _event = _read_value(setting_id, observed_at)
    return record


def preview_value(
    setting_id: str,
    *,
    scope: str,
    value: Any,
    expected_revision: str | None,
    observed_at: datetime | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    """Preview one proposed value using authoritative DST/bridge math.

    No setting row, revision, policy epoch, or event is committed/published.
    """
    _validate_request(setting_id, scope, value, validate_value=True)
    if read_only:
        raise SettingsError(
            "read_only",
            "Dashboard settings are read-only.",
            status_code=403,
        )
    now = _observed_at(observed_at)
    record = _preview_record(setting_id, now)
    _assert_revision(expected_revision, record)
    active_timezone = record["policy_timezone"]
    zone = ZoneInfo(active_timezone)

    proposed_effective_at: datetime | None
    apply_status: str
    if value == record["effective_value"]:
        proposed_effective_at = None
        apply_status = (
            "cancels-pending" if record["pending_value"] is not None else "effective"
        )
    elif value == record["pending_value"] and record["effective_at"]:
        proposed_effective_at = datetime.fromisoformat(record["effective_at"])
        apply_status = "pending"
    else:
        proposed_effective_at = next_safe_boundary_transition(
            now,
            zone,
            record["effective_value"],
            value,
        )
        apply_status = "pending"

    impact = _impact_preview(
        observed_at=now,
        effective_value=record["effective_value"],
        pending_value=value if proposed_effective_at is not None else None,
        pending_effective_at=proposed_effective_at,
        pending_timezone_name=active_timezone,
        last_transition_at=(
            datetime.fromisoformat(record["last_transition"]["applied_at"])
            if record["last_transition"]
            else None
        ),
        active_timezone_name=active_timezone,
    )
    effective_at = (
        proposed_effective_at.astimezone(zone).isoformat()
        if proposed_effective_at is not None
        else None
    )
    return {
        "schema_version": registry.SCHEMA_VERSION,
        "registry_revision": registry.REGISTRY_REVISION,
        "timezone": active_timezone,
        "configured_timezone": _timezone_name(),
        "value_revision": record["revision"],
        "preview": {
            "setting_id": setting_id,
            "scope": record["scope"],
            "value": value,
            "effective_at": effective_at,
            "apply_status": apply_status,
            "impact_preview": impact,
        },
        "diagnostics": record["diagnostics"],
    }


def reset_value(
    setting_id: str,
    *,
    scope: str,
    expected_revision: str | None,
    observed_at: datetime | None = None,
    read_only: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    _validate_request(setting_id, scope)
    now = _observed_at(observed_at)
    current, promotion_event = _read_value(setting_id, now)
    _assert_revision(expected_revision, current)

    default = current["default_value"]
    if current["source"] == "default":
        # No active override: reset only needs to cancel a pending intent.
        if current["pending_value"] is None:
            if read_only:
                raise SettingsError(
                    "read_only",
                    "Dashboard settings are read-only.",
                    status_code=403,
                )
            return current, None
        return _write_pending(
            setting_id=setting_id,
            target_value=current["effective_value"],
            target_source="default",
            expected_revision=current["revision"],
            observed_at=now,
            read_only=read_only,
        )

    return _write_pending(
        setting_id=setting_id,
        target_value=default,
        target_source="default",
        expected_revision=current["revision"],
        observed_at=now,
        read_only=read_only,
    )


def get_journal_day_boundary(
    *, observed_at: datetime | None = None
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    now = _observed_at(observed_at)
    record, event = _read_value(registry.JOURNAL_DAY_BOUNDARY_ID, now)
    return record["effective_value"], record, event


def _policy_window_for_date(
    local_date: date,
    record: dict[str, Any],
) -> JournalDayWindow:
    """Reconstruct one immutable Journal day from durable policy epochs."""
    conn = store.get_connection()
    try:
        base = conn.execute(
            """
            SELECT * FROM journal_day_policy_epoch
            WHERE effective_local_date IS NULL
            LIMIT 1
            """
        ).fetchone()
        epoch = conn.execute(
            """
            SELECT * FROM journal_day_policy_epoch
            WHERE effective_local_date IS NOT NULL
              AND effective_local_date <= ?
            ORDER BY effective_local_date DESC, sequence DESC
            LIMIT 1
            """,
            (local_date.isoformat(),),
        ).fetchone()
        epoch = epoch or base
        if epoch is None:
            raise RuntimeError("Journal day policy history is not initialized")

        timezone_name = epoch["timezone"]
        zone = ZoneInfo(timezone_name)
        boundary = epoch["boundary"]
        if epoch["effective_local_date"] == local_date.isoformat():
            window_start = datetime.fromisoformat(epoch["window_start"]).astimezone(zone)
        else:
            window_start = window_for_local_date(local_date, zone, boundary).start

        next_date = local_date + timedelta(days=1)
        next_epoch = conn.execute(
            """
            SELECT * FROM journal_day_policy_epoch
            WHERE effective_local_date = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (next_date.isoformat(),),
        ).fetchone()
        if next_epoch is not None:
            window_end = datetime.fromisoformat(next_epoch["window_start"]).astimezone(zone)
        else:
            window_end = window_for_local_date(local_date, zone, boundary).end

        # A scheduled later boundary extends the still-open preceding day.
        # Include that planned exact boundary before it becomes a persisted
        # epoch so current reads and impact UI remain contiguous.
        if record["pending_value"] is not None and record["effective_at"]:
            pending_at = datetime.fromisoformat(record["effective_at"])
            pending_zone = ZoneInfo(
                record["pending_timezone"] or record["policy_timezone"]
            )
            pending_date = pending_at.astimezone(pending_zone).date()
            if next_date == pending_date:
                window_end = pending_at.astimezone(zone)

        return JournalDayWindow(
            local_date=local_date,
            timezone=timezone_name,
            boundary=boundary,
            start=window_start,
            end=window_end,
        )
    finally:
        conn.close()


def get_journal_day_window(
    local_date: date | str,
    *,
    observed_at: datetime | None = None,
) -> JournalDayWindow:
    """Return the historical, transition-safe window for a logical date.

    The lookup is policy-history based, not reconstructed from today's setting,
    so changing the boundary does not reinterpret an existing Journal day.
    """
    target_date = (
        date.fromisoformat(local_date) if isinstance(local_date, str) else local_date
    )
    _boundary, record, _event = get_journal_day_boundary(observed_at=observed_at)
    return _policy_window_for_date(target_date, record)


def get_journal_day_binding(
    instant: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    now = _observed_at(instant)
    boundary, record, event = get_journal_day_boundary(observed_at=now)
    conn = store.get_connection()
    try:
        active_epoch = conn.execute(
            """
            SELECT * FROM journal_day_policy_epoch
            ORDER BY (effective_local_date IS NULL) ASC,
                     effective_local_date DESC, sequence DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    zone = ZoneInfo(active_epoch["timezone"]) if active_epoch else wb_config.USER_TZ
    local_date = day_for_instant(now, zone, boundary)

    if record["pending_value"] is not None and record["effective_at"]:
        transition_at = datetime.fromisoformat(record["effective_at"])
        transition_day = transition_at.astimezone(
            ZoneInfo(record["pending_timezone"] or record["policy_timezone"])
        ).date()
        # When the new boundary is later, the old policy would briefly open
        # transition_day before the new boundary arrives. Keep the preceding
        # day open instead, producing one explicit extended bridge window.
        if (
            now.astimezone(timezone.utc)
            < transition_at.astimezone(timezone.utc)
            and local_date >= transition_day
        ):
            local_date = transition_day - timedelta(days=1)
    window = _policy_window_for_date(local_date, record)

    return (
        {
            "local_date": local_date.isoformat(),
            "timezone": window.timezone,
            "day_boundary_start": window.boundary,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "boundary_setting_revision": record["revision"],
            "pending_day_boundary_start": record["pending_value"],
            "boundary_effective_at": record["effective_at"],
            "configured_timezone": record["configured_timezone"],
            "diagnostics": record["diagnostics"],
        },
        event,
    )
