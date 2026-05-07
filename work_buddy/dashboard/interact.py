"""Agent ↔ dashboard form bridge — server side.

The single typed MCP capability ``dashboard_interact`` is the only
thing chat-walkthrough agents call to drive dashboard surfaces. It
dispatches to per-action handlers; right now (step 2 of the build):

  * ``form_field_set`` — validate against the registered FormSchema,
    publish a ``dashboard.form.field_set`` event the frontend bridge
    applies to the matching DOM input.
  * ``form_open`` — publish a ``dashboard.form.open`` event the
    frontend bridge translates into the form's open handler (e.g.
    ``showAddJobForm`` for the Jobs form).

Step 4 will add rendezvous-based actions (``form_submit``,
``form_get_state``). Their entry points are sketched here as
``NotImplementedError`` so the dispatcher's shape is fixed up front.

Validation policy: the capability is strict. Unknown ``form_id``,
unknown field name, wrong type, regex mismatch, enum non-membership
are all errors returned to the agent — not silently published. The
frontend bridge receives only validated events.
"""

from __future__ import annotations

import json
import queue
import re
import secrets
import threading
import time
from typing import Any

from work_buddy.dashboard.forms import FIELD_TYPES, Field, FormSchema, get_schema
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Event names (kept in one place so the frontend bridge subscribes to
# the same strings the capability publishes)
# ---------------------------------------------------------------------------

EVT_FIELD_SET = "dashboard.form.field_set"
EVT_OPEN      = "dashboard.form.open"
EVT_CANCEL    = "dashboard.form.cancel"
EVT_SUBMIT    = "dashboard.form.submit"
EVT_GET_STATE = "dashboard.form.get_state"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _err(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _validate_value(field: Field, value: Any) -> str | None:
    """Return None if value is valid for the field, else an error string."""
    t = field.type
    if t == "str":
        if not isinstance(value, str):
            return f"field {field.name!r}: expected str, got {type(value).__name__}"
        if field.regex and not re.fullmatch(field.regex, value):
            return (
                f"field {field.name!r}: value {value!r} does not match "
                f"required pattern {field.regex!r}"
            )
        return None
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"field {field.name!r}: expected int, got {type(value).__name__}"
        return None
    if t == "bool":
        if not isinstance(value, bool):
            return f"field {field.name!r}: expected bool, got {type(value).__name__}"
        return None
    if t == "cron":
        if not isinstance(value, str):
            return f"field {field.name!r}: expected str (cron), got {type(value).__name__}"
        from work_buddy.sidecar.scheduler.cron import parse_cron_field
        parts = value.strip().split()
        if len(parts) != 5:
            return (
                f"field {field.name!r}: cron must be 5 fields "
                f"(MIN HOUR DOM MON DOW), got {len(parts)}"
            )
        ranges = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
        for i, (part, (lo, hi)) in enumerate(zip(parts, ranges)):
            if not parse_cron_field(part, lo, hi):
                return (
                    f"field {field.name!r}: cron field #{i+1} ({part!r}) is invalid"
                )
        return None
    if t == "enum":
        if value not in field.enum_values:
            return (
                f"field {field.name!r}: value {value!r} not in "
                f"{list(field.enum_values)}"
            )
        return None
    if t == "dict":
        if not isinstance(value, dict):
            return f"field {field.name!r}: expected dict, got {type(value).__name__}"
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            return f"field {field.name!r}: dict is not JSON-serializable ({exc})"
        return None
    # Should be unreachable thanks to FIELD_TYPES guard in Field.__post_init__.
    return f"field {field.name!r}: unknown type {t!r}"


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _action_form_field_set(
    schema: FormSchema, field_name: str, value: Any,
) -> dict[str, Any]:
    field = schema.field(field_name)
    if field is None:
        return _err(
            f"field {field_name!r} not declared on schema {schema.form_id!r}. "
            f"Valid fields: {[f.name for f in schema.fields]}"
        )
    err = _validate_value(field, value)
    if err is not None:
        return _err(err)

    from work_buddy.dashboard.events import publish_auto
    publish_auto(EVT_FIELD_SET, {
        "form_id": schema.form_id,
        "field": field_name,
        "value": value,
    })
    return {"ok": True}


def _action_form_open(schema: FormSchema) -> dict[str, Any]:
    from work_buddy.dashboard.events import publish_auto
    publish_auto(EVT_OPEN, {"form_id": schema.form_id})
    return {"ok": True}


def _action_form_cancel(schema: FormSchema) -> dict[str, Any]:
    """Click the form's Cancel button on the user's behalf.

    Used by chat-walkthrough agents when the user explicitly aborts
    mid-flow (e.g. after a confirmation prompt). Fires the registered
    ``cancelHandler`` on the frontend, which typically clears the form
    inputs and hides it. Fire-and-forget — no rendezvous; the user-
    visible form state is the confirmation.
    """
    from work_buddy.dashboard.events import publish_auto
    publish_auto(EVT_CANCEL, {"form_id": schema.form_id})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Rendezvous — synchronous request/response between the capability and the
# frontend. Used by ``form_submit`` and ``form_get_state``.
#
# Flow per call:
#   1. Capability creates a queue keyed by a fresh request_id.
#   2. Capability publishes the event with that request_id on the bus.
#   3. Frontend receives the event, runs its handler, POSTs the result to
#      ``/api/dashboard/interact/result/<request_id>`` (handled in
#      ``service.py``, which calls :func:`deliver_result`).
#   4. Capability blocks on the queue, returns the posted result, or
#      times out and returns ``{ok: false, error: "timeout"}``.
#
# Stale entries are evicted by the periodic sweeper below.
# ---------------------------------------------------------------------------

_pending: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()
_SWEEP_INTERVAL_S = 30.0
_SWEEP_MAX_AGE_S = 120.0  # an entry older than this is definitely orphaned


def _open_rendezvous() -> tuple[str, "queue.Queue[dict[str, Any]]"]:
    request_id = secrets.token_hex(8)
    q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
    with _pending_lock:
        _pending[request_id] = {"queue": q, "created_at": time.time()}
    return request_id, q


def _close_rendezvous(request_id: str) -> None:
    with _pending_lock:
        _pending.pop(request_id, None)


def deliver_result(request_id: str, payload: dict[str, Any]) -> bool:
    """Called by ``POST /api/dashboard/interact/result/<request_id>``.

    Looks up the queue for the request_id, places the payload, and
    returns whether the rendezvous existed. Unknown / expired
    request_ids return False so the endpoint can 404.
    """
    with _pending_lock:
        entry = _pending.pop(request_id, None)
    if entry is None:
        return False
    try:
        entry["queue"].put_nowait(payload)
        return True
    except queue.Full:
        return False


def _sweeper_loop() -> None:
    """Drop rendezvous entries older than _SWEEP_MAX_AGE_S.

    The capability times out by itself, but if its calling thread
    dies (e.g. the MCP gateway restarts mid-call), the queue is
    orphaned. This keeps _pending bounded.
    """
    while True:
        try:
            time.sleep(_SWEEP_INTERVAL_S)
            cutoff = time.time() - _SWEEP_MAX_AGE_S
            with _pending_lock:
                stale = [
                    rid for rid, entry in _pending.items()
                    if entry.get("created_at", 0) < cutoff
                ]
                for rid in stale:
                    _pending.pop(rid, None)
            if stale:
                logger.info(
                    "dashboard_interact: swept %d stale rendezvous entries",
                    len(stale),
                )
        except Exception:
            logger.exception("dashboard_interact sweeper iteration failed")


_sweeper_started = False
_sweeper_lock = threading.Lock()


def _ensure_sweeper() -> None:
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        threading.Thread(
            target=_sweeper_loop, name="dashboard_interact_sweeper",
            daemon=True,
        ).start()
        _sweeper_started = True


def _action_form_submit(
    schema: FormSchema, timeout_seconds: float,
) -> dict[str, Any]:
    _ensure_sweeper()
    request_id, q = _open_rendezvous()
    from work_buddy.dashboard.events import publish_auto
    publish_auto(EVT_SUBMIT, {
        "form_id": schema.form_id,
        "request_id": request_id,
    })
    try:
        result = q.get(timeout=max(0.5, float(timeout_seconds)))
    except queue.Empty:
        _close_rendezvous(request_id)
        return {
            "ok": False,
            "error": (
                f"form_submit timed out after {timeout_seconds}s — "
                f"the frontend bridge did not return a result. The form "
                f"may not be open, the user may have closed the dashboard, "
                f"or the form's submit handler may have hung."
            ),
        }
    return result if isinstance(result, dict) else {"ok": False, "error": "non-dict result"}


def _action_form_get_state(
    schema: FormSchema, timeout_seconds: float,
) -> dict[str, Any]:
    _ensure_sweeper()
    request_id, q = _open_rendezvous()
    from work_buddy.dashboard.events import publish_auto
    publish_auto(EVT_GET_STATE, {
        "form_id": schema.form_id,
        "request_id": request_id,
    })
    try:
        result = q.get(timeout=max(0.5, float(timeout_seconds)))
    except queue.Empty:
        _close_rendezvous(request_id)
        return {
            "ok": False,
            "error": (
                f"form_get_state timed out after {timeout_seconds}s — "
                f"the form may not be mounted in the user's browser."
            ),
        }
    return result if isinstance(result, dict) else {"ok": False, "error": "non-dict result"}


# ---------------------------------------------------------------------------
# Capability entry point
# ---------------------------------------------------------------------------

def dashboard_interact(
    action: str,
    form_id: str,
    field: str = "",
    value: Any = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Drive a dashboard form on the user's behalf.

    Single typed entry point for chat-walkthrough agents. Each action
    is validated against the form's :class:`FormSchema` before it
    publishes anything on the bus, so the frontend bridge only ever
    sees well-formed events.

    Args:
        action: One of ``form_field_set``, ``form_open``,
            ``form_submit``, ``form_get_state``.
        form_id: Form to address. Must be a registered schema.
        field: Field name (only for ``form_field_set``).
        value: Field value (only for ``form_field_set``). Type checked
            against the field's declared type.
        timeout_seconds: Rendezvous timeout for ``form_submit`` /
            ``form_get_state`` (step 4). Ignored for other actions.

    Returns:
        ``{ok: True}`` on success, ``{ok: False, error: str}`` on
        validation failure. ``form_submit`` and ``form_get_state``
        will additionally return typed payloads once step 4 lands.
    """
    schema = get_schema(form_id)
    if schema is None:
        return _err(
            f"form_id {form_id!r} is not a registered FormSchema"
        )

    if action == "form_field_set":
        if not field:
            return _err("form_field_set requires 'field'")
        return _action_form_field_set(schema, field, value)
    if action == "form_open":
        return _action_form_open(schema)
    if action == "form_cancel":
        return _action_form_cancel(schema)
    if action == "form_submit":
        return _action_form_submit(schema, timeout_seconds)
    if action == "form_get_state":
        return _action_form_get_state(schema, timeout_seconds)

    return _err(
        f"unknown action {action!r}. Valid: form_field_set, form_open, "
        f"form_cancel, form_submit, form_get_state"
    )
