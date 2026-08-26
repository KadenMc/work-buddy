---
name: Widget-native assisted drafts
kind: concept
description: Advisory conversations that patch declared host drafts without acquiring submission, DOM, or domain authority.
summary: A Dashboard-owned assistance dock binds one full WidgetDraftIdentity, reuses house conversation/chat transport, and applies canonical-schema patches only through the mounted draft CAS handle.
tags:
- dashboard
- react
- drafts
- conversations
- security
aliases:
- assisted form
- Help me shape this
- form assistance
parents:
- services/dashboard/react
entry_points:
- dashboard-react/src/dashboard/assistance/AssistedDraftRuntime.tsx
- work_buddy/dashboard/assistance/api.py
- work_buddy/dashboard/assistance/form_schemas.json
---

## Authority and opt-in

The mounted Dashboard widget owns the recoverable current draft. Conversations
owns transcript and driver lease; the assistance broker owns immutable per-turn
snapshot/patch evidence and host acknowledgements. It is not a second draft
store, task/job writer, or proposal authority.

A widget declares a normal `drafts` entry and adds
`assistableDrafts: [assistedDraftDeclaration("task-create")]` (or `job-create`).
Its renderer calls `useAssistedDraft(draftName, handle, options)`, renders
`AssistDraftButton`, and spreads `fieldProps(["field"])` on the actual controls.
The full identity includes profile, workspace, app, view, widget instance/type,
draft name, and scope key. Non-assistable metadata remains in the host draft.

The sole field authority is
`work_buddy/dashboard/assistance/form_schemas.json`, read by Python and imported
directly by TypeScript. It declares types, bounds, descriptions, disclosure,
required/optional fields, default values, operations, patch behavior, and
`submitPolicy: user_only`. There is no parallel DOM-field map. Remove resets an
optional field to its declared empty/default value; required fields cannot be
removed. Batch lines and proposal bindings are not assistable fields. Jobs
parameter JSON refuses credential-shaped keys before disclosure.

## Conversation and disclosure

`AssistedDraftRuntimeProvider` owns a contextual, non-modal dock attached to the
form. Its shared layout reserves a sibling desktop column; on narrow screens,
separate scrollable form and conversation rows keep the real submit controls
reachable. Opening/closing it never remounts the form or moves keyboard focus.
It composes `ConversationChat`/`ChatPanel`, not the Co-work adapter and not
a separately placeable chat widget. The existing `HttpChatConversationProvider`
uses an assistance-scoped base path with the normal house conversation wire
shape. Its shared message ID is allocated before `prepareSend`. The host stages
one immutable, allowlisted revision/hash snapshot under that ID, preserving it
even when preparation acknowledgement is uncertain. Field values and revision
freeze synchronously at Send, before waiting for persistence; later edits do
not expand that turn's disclosure and are protected by normal patch conflicts.

No model starts on form rendering or Help. The Settings registry defaults
`wb.dashboard.assistance` to disabled; `wb.dashboard.assistance-tier` chooses a
preflightable no-tools inference tier. The dock exposes provider, model,
purpose, context bounds, and a visible Settings/retry action. The explicit Start
gesture binds the provider/model displayed in that disclosure. An authored chat
turn invokes one bounded structured `LLMRunner` call, with no tools or fallback
provider. The source-bound execution gateway retains the exact dynamic context
and writes its disclosure manifest before handoff. Current support is a concrete
Anthropic inference tier, not an implicit account-backed or local-provider
fallback.

The broker appends canonical Conversations user/agent messages and uses its
existing lease claim, inbox, write guard, acknowledgement cursor, and stop
operations. The same user-message replay returns the same durable reply and
patch ID. Invalid model output produces a readable, sanitized recovery message
and no patch. Generic legacy conversation routes reject these bound sessions;
only the protected assistance routes can access them.

## Patch and recovery laws

A patch binds assistant session, conversation, full draft identity, schema,
base revision, SHA-256 base snapshot, stable patch ID, and allowlisted path-array
set/remove operations. Malformed, oversize, secret, unknown, root, wildcard, or
prototype-polluting operations reject the entire patch. There are no submit,
DOM selector, arbitrary callback, or navigation operations.

After validation, the host flushes queued saves and takes a fresh synchronous
snapshot. A stale patch is evaluated field-by-field: unchanged and unfocused
fields apply in one local revision; changed or focused fields become visible
suggestions. The normal draft repository CAS fences another tab's saves. Applied
and pending fields appear in a durable acknowledgement, visual non-color marks,
and a polite announcement. Applying suggestions is an explicit user review.
Undo restores a field only while it still contains the assistant's applied
value and is not focused.

The host retains bounded patch receipts and inverses in tab-scoped session
storage keyed by full draft identity and assistant session. It journals the
planned inverse before CAS and records completion after flush. A remount restores
the journal before polling; an interrupted local write is inspected against
current fields, never replayed blindly. A missing server acknowledgement is
retried without applying the patch again. These receipts are evidence, not
another current draft. A panel close preserves the conversation and Undo. Ending
the session preserves the form but removes its active assistant binding.

An accepted host `draft.clear()` or `draft.reset(value, { ifRevision })`
synchronously notifies reset subscribers. Assistance invalidates its editing
generation, closes the dock, and removes the saved session binding before a
late reply can cross into the next editing lifetime. Reset preserves explicitly
chosen fields with one normal repository CAS replacement save; it never deletes
the old durable draft before saving the replacement. Clear retains its existing
delete behavior. A failed revision check does not revoke the session, and a
failed replacement save leaves the previous durable record recoverable.

Operate-only checks exist both in the host and in the server-owned dashboard
read-only callback, including before model handoff/result publication. Arrange,
Preview, or read-only transitions pause sends and patches. Unmount/reset fences
late operations. Startup/storage/model failure never disables ordinary form
editing or replaces the form's normal human submit intent.

Manual form editing remains available in Preview through the existing host's
forked draft repository. Normal task/job submissions stop at the host's declared
Preview effect policy before reaching a provider. Proposal saves, acceptance,
dismissal and retries are separately Operate-only, including before pending
request metadata is allocated. Leaving Preview restores the original draft;
Arrange and read-only access continue to lock the form.

## Verification

`tests/unit/test_dashboard_assistance.py` exercises isolated HTTP conversation →
patch → acknowledgement flow, idempotency, cross-binding authorization, server
read-only, malformed/secret fields, expiration, stop/resume, and actual
source-bound disclosure ordering with an injected deterministic model runner.
React assistance tests exercise live form fields, focused/stale conflicts,
conditional Undo, retained receipts across remount, retry identity, mode gates,
and clear-generation fencing. The shared ChatComposer additionally tests that a
delayed acknowledgement never reclaims focus from another host control.
