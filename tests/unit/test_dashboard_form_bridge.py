"""Contract test for the dashboard form-bridge subsystem.

Asserts every ``Field.ui_id`` declared in any registered ``FormSchema``
exists as an element id in the rendered dashboard HTML. Without this,
schemas drift silently from the DOM and the chat-walkthrough agent's
``dashboard_interact("form_field_set", ...)`` calls land on empty
selectors, with no error surfaced anywhere.

CI fails the moment a schema's ``ui_id`` no longer matches reality —
either a renamed input or a deleted form. The fix is one of:
  * Update the schema to the new ui_id.
  * Restore the missing element in ``html.py``.
  * Remove the schema if the form is gone.
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def rendered_html() -> str:
    # ``work_buddy.dashboard`` auto-imports every forms_*.py module so
    # ``register_schema`` runs at import time. ``render_page`` builds
    # the full HTML the browser sees.
    import work_buddy.dashboard  # noqa: F401  — triggers schema registration
    from work_buddy.dashboard.frontend import assembled_js, render_page

    # Field ui_ids may live in the static skeleton (html.py) or in JS-built
    # forms (now served as an external asset), so check both.
    return render_page() + "\n" + assembled_js()


def test_every_schema_field_ui_id_exists_in_rendered_page(rendered_html: str) -> None:
    from work_buddy.dashboard.forms import all_schemas

    schemas = all_schemas()
    assert schemas, "no FormSchemas registered — forms_jobs import is broken"

    failures: list[str] = []
    for schema in schemas:
        for field in schema.fields:
            if not field.ui_id:
                continue
            # Match either id="..." or id='...' to be robust to the
            # quoting style used in the inline HTML strings.
            pattern = re.compile(
                rf'id=["\']{re.escape(field.ui_id)}["\']'
            )
            if not pattern.search(rendered_html):
                failures.append(
                    f"  - {schema.form_id}.{field.name}: "
                    f"ui_id={field.ui_id!r} not found in rendered page"
                )

    if failures:
        pytest.fail(
            "FormSchema ui_id ↔ DOM contract violation:\n"
            + "\n".join(failures)
            + "\n\nFix: update the schema's ui_id, restore the missing "
            "element, or delete the obsolete schema."
        )


def test_jobs_schema_is_registered() -> None:
    """Sanity check — JOBS_FORM_SCHEMA registers via the auto-import."""
    import work_buddy.dashboard  # noqa: F401
    from work_buddy.dashboard.forms import get_schema

    schema = get_schema("jobs-add-job")
    assert schema is not None, "jobs-add-job schema should be registered"
    assert schema.submit_label == "Create job"
    field_names = {f.name for f in schema.fields}
    assert {"name", "schedule", "job_type"} <= field_names
