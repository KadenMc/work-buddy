"""Dashboard form schemas — the single source of truth.

Each consumer of the agent↔form bridge declares a ``FormSchema`` here
(or in a sibling module like ``forms_jobs.py``). The schema is read by:

  * The brief renderer (``interact_brief.render_form_section``) to
    generate the structural prose appended to chat-walkthrough agents'
    starter prompts. Agents never see field IDs hand-written into prose.
  * The ``dashboard_interact`` MCP capability to validate the form_id,
    field name, and value type/regex before publishing the bus event.
  * The frontend ``window.wbFormBridge`` to wire field handlers, the
    submit handler, and the open handler — one registration per form.
  * The contract test (``test_dashboard_form_bridge.py``) which asserts
    every declared ``ui_id`` appears in the rendered dashboard HTML.

A schema is registered with ``register_schema`` at import time; the
schema modules (``forms_jobs``, future ``forms_contracts``, …) are
auto-imported by ``work_buddy.dashboard`` so the registry is populated
before any consumer queries it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Field type identifiers the bridge understands. Free-form ``str`` is
# the default; the others enable extra validation in the capability
# (cron field-count check, enum membership check, etc.).
FIELD_TYPES = frozenset({
    "str",
    "int",
    "bool",
    "cron",     # 5-field cron expression (validated via parse_cron_field)
    "enum",     # value must be in the field's enum_values tuple
    "dict",     # arbitrary JSON-serializable mapping
})


@dataclass(frozen=True)
class Field:
    """One declared form field.

    Attributes:
        name: Canonical key the agent uses to address this field
            (e.g. ``"schedule"``). Stable; never renamed.
        type: One of :data:`FIELD_TYPES`. Drives validation in the
            ``dashboard_interact`` capability.
        ui_id: DOM element id this field maps to in the rendered page.
            The contract test asserts this id exists; the frontend
            bridge uses it to dispatch ``form_field_set`` events.
        required: Whether the form rejects submission without this
            field set. Surfaced in the brief.
        description: One-line description rendered into the brief and
            shown to the agent verbatim.
        enum_values: For ``type == "enum"``, the allowed values.
        regex: For ``type == "str"``, a regex the value must fully
            match. Surfaced in the brief; not enforced by the bridge
            (the form's own submit validation is authoritative).
    """
    name: str
    type: str
    ui_id: str
    required: bool = False
    description: str = ""
    enum_values: tuple[str, ...] = ()
    regex: str = ""

    def __post_init__(self) -> None:
        if self.type not in FIELD_TYPES:
            raise ValueError(
                f"Field {self.name!r}: unknown type {self.type!r} "
                f"(valid: {sorted(FIELD_TYPES)})"
            )
        if self.type == "enum" and not self.enum_values:
            raise ValueError(
                f"Field {self.name!r}: type=enum requires enum_values"
            )


@dataclass(frozen=True)
class FormSchema:
    """One declared form, addressable by ``form_id``.

    The form_id is the stable identifier agents use; the description
    is rendered into the brief so the agent knows what kind of thing
    the form creates. The submit_label is the user-visible button
    label, surfaced in the brief so the agent can refer to it
    naturally ("when you click Create job…").
    """
    form_id: str
    description: str
    fields: tuple[Field, ...]
    submit_label: str = "Submit"

    def field(self, name: str) -> Field | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_SCHEMAS: dict[str, FormSchema] = {}


def register_schema(schema: FormSchema) -> None:
    """Register a form schema. Called at module import time.

    Re-registration with the same form_id is allowed (overwrites);
    that lets test fixtures replace a schema without an import dance.
    """
    _SCHEMAS[schema.form_id] = schema


def get_schema(form_id: str) -> FormSchema | None:
    return _SCHEMAS.get(form_id)


def all_schemas() -> list[FormSchema]:
    return list(_SCHEMAS.values())
