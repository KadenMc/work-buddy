---
name: Dashboard Form Bridge
kind: concept
description: 'Schema-driven agent ↔ form interaction subsystem: one MCP capability, one frontend bridge, one contract test asserting schema↔DOM stay in sync.'
tags:
- dashboard
- forms
- bridge
- schema
- reusable
- agent-interaction
- interact
aliases:
- wbFormBridge
- dashboard_interact
- FormSchema
- form bridge
- agent form interaction
- schema-driven form
parents:
- services/dashboard
- services/dashboard
---

Schema-driven layer between chat-walkthrough agents and the dashboard's forms. Agents call **one** typed MCP capability — ``dashboard_interact`` — and the bridge routes each action to per-form handlers registered on the frontend.

## Three load-bearing pieces

* **`FormSchema`** (``work_buddy/dashboard/forms.py``) — single source of truth. Each form declares its ``form_id``, fields (name/type/ui_id/required/regex/enum), and submit_label. Schemas live in ``forms_<consumer>.py`` modules auto-imported by ``work_buddy/dashboard/__init__.py``. The schema is read by the brief renderer, the capability validator, the frontend bridge, and the contract test.
* **`dashboard_interact` MCP capability** (``work_buddy/dashboard/interact.py`` + ``mcp_server/registry.py``) — actions: ``form_field_set``, ``form_open``, ``form_cancel``, ``form_submit``, ``form_get_state``. The capability is a thin HTTP wrapper that POSTs to ``/api/dashboard/interact``; the actual logic runs in the dashboard process where the rendezvous map shares memory with the result-postback endpoint.
* **`window.wbFormBridge`** (``work_buddy/dashboard/frontend/scripts/core/form_bridge.py``) — frontend half. Each form calls ``register(form_id, {fieldHandlers, openHandler, cancelHandler, submitHandler, getStateHandler})`` once; the bridge subscribes to ``dashboard.form.*`` events and dispatches to the matching handler.

## Action semantics

* **`form_field_set`** — fire-and-forget. Validates the field name against the schema, validates the value against the field's declared type/regex/enum, publishes ``dashboard.form.field_set`` on the event bus. Returns ``{ok: true}`` on success or a typed error.
* **`form_open`** — fire-and-forget. Publishes ``dashboard.form.open``; the registered ``openHandler`` is responsible for whatever "open the form" means for that consumer (typically un-collapsing a hidden ``<form>``).
* **`form_cancel`** — fire-and-forget. Publishes ``dashboard.form.cancel``; the registered ``cancelHandler`` clears + hides the form. Used by the chat agent only when the user explicitly opts out.
* **`form_submit`** — synchronous **rendezvous**. The capability publishes ``dashboard.form.submit {form_id, request_id}`` and blocks on a queue keyed by ``request_id`` for up to ``timeout_seconds``. The frontend bridge invokes the registered ``submitHandler`` (which runs the form's existing submit code path), then POSTs the result to ``/api/dashboard/interact/result/<request_id>``. The endpoint hands the payload back to the queue; the capability returns ``{ok: bool, error?: str, errors_by_field?: dict, suggestions?: [str]}``.
* **`form_get_state`** — synchronous rendezvous. Same shape as ``form_submit`` but the registered ``getStateHandler`` returns the form's current field values. Used when an agent resumes a conversation and wants to know what's already filled in.

## Typed error shape

Server-side validators (``create_user_job_file`` for the Jobs form; sibling helpers for future consumers) return a uniform error shape on failure:

```
{
  "ok": false,
  "error": "<human-readable summary>",
  "errors_by_field": {"<field_name>": "<message>", ...},
  "suggestions": ["<closest-match>", ...]
}
```

* The frontend uses ``errors_by_field`` to paint the offending input red (``.jobs-form-field-invalid``); editing the input clears the highlight.
* The chat agent uses ``errors_by_field`` to know which specific field to re-prompt for, and ``suggestions`` to seed the corrected value (with ``wb_search`` verification) before retrying.
* The summary ``error`` string is shown verbatim in the form's error div.

## Slash-command-aware name validation

Users and agents often remember the slash-command name (``/wb-morning``, ``wb-morning``, or just ``morning``) rather than the underlying registry entry (``morning-routine``). The Jobs form validator handles all four spellings:

* The submit path strips a leading ``/`` and trailing whitespace.
* The typeahead surfaces ONE card per registry entry whose value is the canonical name and whose label includes ``/<slash>`` and the description, so typing either name matches the same card. On submit, slash-command aliases auto-resolve to the canonical name.
* The server-side validator prioritizes a slash-command match in its error message: if ``wb-morning`` is the slash command for ``morning-routine``, the rejection explicitly says so and lists the canonical name in ``suggestions``. ``difflib`` close-match is the fallback.

## Why the rendezvous lives in the dashboard process

The MCP gateway runs capabilities in the gateway process. ``publish_auto`` is cross-process (events from the gateway are bridged into the dashboard's bus via the messaging service). But the **result-postback** is an HTTP call from the user's browser to the dashboard process — it lands in the dashboard's memory, not the gateway's.

If the rendezvous map (``_pending``) lived in the capability's calling process, the dashboard's ``deliver_result`` would never find the matching queue entry. Routing the capability through the dashboard endpoint puts both halves of the transaction in the same process.

The MCP capability is therefore a thin HTTP forwarder. The dashboard's ``api_dashboard_interact`` endpoint owns the transaction; the capability is a typed surface for agents.

## Programmatic brief injection

Agent briefs are split into two halves:

* **Static prose** — per consumer (``jobs_help.py`` has its own; future ``contracts_help.py`` would have its own). Describes role, conversational style, ban on direct underlying-store writes. ~30-50 lines.
* **Generated structural section** — produced by ``interact_brief.render_form_section(schema)``. Lists every field with type/required/regex/enum/description, plus concrete ``dashboard_interact`` example calls (``form_field_set``, ``form_open``, ``form_cancel``, ``form_submit``, ``form_get_state``) pre-filled with the form's ``form_id``.

No input ids in the brief. No hand-written JSON shapes. Adding a field to the schema automatically updates every spawned agent's prompt on the next session start.

## Adding a new consumer

1. Declare the schema in ``work_buddy/dashboard/forms_<name>.py`` and call ``register_schema``.
2. Add the import line to ``work_buddy/dashboard/__init__.py``.
3. In the form's tab module, call ``window.wbFormBridge.register('<form_id>', {fieldHandlers, openHandler, cancelHandler, submitHandler, getStateHandler})``.
4. Write a static prose preamble in the consumer's brief module and use ``render_form_section`` for the structural tail.
5. Run the contract test — it'll fail if any ``ui_id`` doesn't exist in the rendered page.

Approx. 30-50 lines per consumer total, almost all of it the schema declaration and the prose preamble.

## Contract test

``tests/unit/test_dashboard_form_bridge.py`` parses the rendered dashboard HTML and asserts every registered ``Field.ui_id`` appears as an element id. Fails CI on schema↔DOM drift. The fix is one of: update the schema's ui_id, restore the missing element, or delete the obsolete schema.

## Endpoints

* ``POST /api/dashboard/interact`` — the typed entry point used by the MCP capability and any other process. Body ``{action, form_id, field?, value?, timeout_seconds?}``. Read-only-mode-gated.
* ``POST /api/dashboard/interact/result/<request_id>`` — the frontend's postback for rendezvous-backed actions. Body ``{ok, error?, errors_by_field?, fields?}``. Returns 404 if no rendezvous is pending for that request_id (the timeout already fired).

## First consumer

The Jobs tab's ``💬 Help me fill this out`` button + the Add-job form. See ``work_buddy/dashboard/jobs_help.py`` for the consumer-specific prose preamble and the spawn orchestrator. Adding ``💬 Help me create a contract`` (or any other agent-driven form) follows the same pattern.

## What this subsystem is NOT

* Not a generic remote-control protocol. Actions operate on declared forms only — no ``click_button(selector)`` or ``type_into_dom(selector, text)``.
* Not a wizard / multi-step form coordinator. One form per registration; multi-step flows would require a separate primitive.
* Not auto-discovery for agents. Agents are told their one form_id in the brief; ``form_get_state`` and ``form_field_set`` validate the form_id but the bridge doesn't expose a list-all-forms surface.
