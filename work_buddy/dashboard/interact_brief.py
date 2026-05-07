"""Brief-section renderer — schema → structural prose.

Converts a :class:`FormSchema` into a markdown block that gets
appended to chat-walkthrough agents' starter prompts. The brief
becomes:

    STATIC_PROSE  (per consumer; describes role, conversational style,
                   ban on direct file writes)
        +
    render_form_section(schema)  (generated; describes form_id, every
                                  field's name/type/constraint, and
                                  the exact dashboard_interact calls
                                  the agent should make)

This is the only place agent-readable structural detail about forms
lives. Adding a new field to a schema automatically updates every
spawned agent's brief on the next session start.
"""

from __future__ import annotations

import json

from work_buddy.dashboard.forms import Field, FormSchema


def _field_constraints(field: Field) -> str:
    """Render the type-specific constraint blurb for a field."""
    parts: list[str] = [f"type=`{field.type}`"]
    if field.required:
        parts.append("**required**")
    if field.enum_values:
        parts.append(f"one of: {list(field.enum_values)!r}")
    if field.regex:
        parts.append(f"regex: ``{field.regex}``")
    return ", ".join(parts)


def render_form_section(schema: FormSchema) -> str:
    """Return the markdown block describing this form to an agent."""
    lines: list[str] = []
    lines.append("## Form you are driving")
    lines.append("")
    lines.append(f"**form_id:** ``{schema.form_id}``")
    lines.append("")
    lines.append(schema.description)
    lines.append("")
    lines.append(
        f"The user-visible submit button on this form is labeled "
        f"**{schema.submit_label}**."
    )
    lines.append("")

    lines.append("### Fields")
    lines.append("")
    for f in schema.fields:
        lines.append(f"- **`{f.name}`** — {_field_constraints(f)}")
        if f.description:
            lines.append(f"    - {f.description}")
    lines.append("")

    lines.append("### How to drive the form")
    lines.append("")
    lines.append(
        "All form interaction goes through the single MCP capability "
        "``dashboard_interact``. Validation happens in the capability — "
        "you receive a typed error if you address an unknown field, "
        "wrong value type, or invalid value. The frontend never sees "
        "an unvalidated event."
    )
    lines.append("")

    # Pick a concrete sample field for the field_set example: the
    # first non-required field's not great because it might have a
    # weird type. Use the first field instead.
    sample = schema.fields[0] if schema.fields else None

    lines.append("**Set a field's value** (call after each piece of info "
                 "the user confirms — they watch the form populate live):")
    lines.append("")
    if sample is not None:
        sample_value = _sample_value(sample)
        lines.append("```")
        lines.append(
            f'mcp__work-buddy__wb_run("dashboard_interact", '
            f'{{"action": "form_field_set", "form_id": "{schema.form_id}", '
            f'"field": "{sample.name}", "value": {json.dumps(sample_value)}}})'
        )
        lines.append("```")
        lines.append("")

    lines.append(
        "**Open the form** (call once near the start, in case the user "
        "hasn't expanded it yet):"
    )
    lines.append("")
    lines.append("```")
    lines.append(
        f'mcp__work-buddy__wb_run("dashboard_interact", '
        f'{{"action": "form_open", "form_id": "{schema.form_id}"}})'
    )
    lines.append("```")
    lines.append("")

    lines.append(
        "**Cancel the form** (only when the user has explicitly said "
        "they want to abort — confirm first via `conversation_ask`). "
        "Clears the form inputs and hides it, same as the user "
        "clicking Cancel:"
    )
    lines.append("")
    lines.append("```")
    lines.append(
        f'mcp__work-buddy__wb_run("dashboard_interact", '
        f'{{"action": "form_cancel", "form_id": "{schema.form_id}"}})'
    )
    lines.append("```")
    lines.append("")

    lines.append(
        "**Submit the form** (call once after the user confirms the "
        "summary; this clicks the **{label}** button on their behalf "
        "and goes through the dashboard's own submit flow — DO NOT "
        "write the underlying file directly):".format(label=schema.submit_label)
    )
    lines.append("")
    lines.append("```")
    lines.append(
        f'mcp__work-buddy__wb_run("dashboard_interact", '
        f'{{"action": "form_submit", "form_id": "{schema.form_id}"}})'
    )
    lines.append("```")
    lines.append("")
    lines.append(
        "The capability blocks (default 10s) until the form's submit "
        "handler returns. Returns ``{ok: true}`` on success or "
        "``{ok: false, error: \"…\", errors_by_field: {field: msg, …}}`` "
        "on validation failure. On error, fix the offending field via "
        "another ``form_field_set`` and re-submit."
    )
    lines.append("")

    lines.append(
        "**Read current form state** (rarely needed; useful when "
        "resuming a conversation):"
    )
    lines.append("")
    lines.append("```")
    lines.append(
        f'mcp__work-buddy__wb_run("dashboard_interact", '
        f'{{"action": "form_get_state", "form_id": "{schema.form_id}"}})'
    )
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def _sample_value(field: Field) -> object:
    """Pick a syntactically-valid example value for a field."""
    if field.enum_values:
        return field.enum_values[0]
    if field.type == "cron":
        return "0 9 * * 1-5"
    if field.type == "int":
        return 1
    if field.type == "bool":
        return True
    if field.type == "dict":
        return {}
    return "example"
