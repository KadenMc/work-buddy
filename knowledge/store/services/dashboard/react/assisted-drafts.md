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
- "assisted form"
- "Help me shape this"
- "form assistance"
- "AI help"
parents:
- services/dashboard/react
entry_points:
- dashboard-react/src/dashboard/assistance/AssistedDraftRuntime.tsx
- work_buddy/dashboard/assistance/api.py
- work_buddy/dashboard/assistance/form_schemas.json
children:
- "services/dashboard/react/assisted-draft-context-get"
- "services/dashboard/react/assisted-draft-propose-patch"
dev_notes: |-
  `AssistedDraftRuntimeProvider` keeps session/cancellation authority at the root;
  `AssistedDraftWorkspace` renders the matching dock inside ViewHost's content
  boundary under the existing Help provider. A root-owned React portal would not
  inherit that destination context. An opaque outlet token supplements (never
  replaces) the full draft identity, so another view or preview cannot claim it.

  Workspace layout uses `WorkspaceSidePanel`, not Chat-owned resize code. Closed
  or compact-inactive panes remain mounted and inert. True view/instance removal,
  draft reset, and scope changes still fence or revoke assistance. The view host
  keeps assistable form renderers alive across grid/mobile presentation changes
  without granting durable-widget persistence or bypassing Arrange/Preview safety.

  `composerPrimaryAction` maps the existing start protocol to the visible Launch
  action. HTTP Start payloads, control revisions, frozen retry identities, scoped
  Stop, and human-only submission authority are unchanged. Browser regression
  must cross the actual ViewHost media breakpoint, not only a mocked panel width,
  and exercise multiline drafts plus expanded recovery controls in short windows.
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

The entry action is the shared duotone sparkle icon and **AI help**. Its
non-modal side panel sits beside the view's grid content, below page headings
and navigation. Drag the shared orange divider, use its Left/Right arrow keys,
or double-click to restore default widths. The view remembers the chosen width.
Narrow or short workspaces switch between **Form** and **AI help** while keeping
both mounted. Resizing or closing preserves the form, conversation and unsent
message. Closing returns focus to the originating action; asynchronous replies
do not steal focus from the form.

The existing Dashboard Hover Help mode also covers the panel heading, close
action, model/context controls, Launch and resize divider. Optional explanations
stay in help; required disclosure, errors and recovery remain visible. The panel
keeps its controls reachable when a long draft or recovery message needs more
space than the viewport.

The dock composes the same `ConversationChat`, `ChatPanelState`, composer,
message/choice rendering, execution hook and picker used by Co-work. It is not
a separately placeable chat widget and does not import Co-work document state.
Form context, explicit lifecycle actions and patch receipts use narrow host
accessories. The conversation provider remains stable across lifecycle and
model changes, preserving the transcript and unsent composer draft.

**System → Dashboard AI** owns the `wb.dashboard.assistance` opt-in and
`wb.dashboard.chat-execution-default` for new chats. A prepared conversation
pins its own provider/model pair; its picker never changes the global default.
Internal inference tiers are not interactive model choices. Claude Code and
Codex are registered interactive providers; a local inference profile is not a
local agent driver, and there is no hidden provider fallback.

Opening AI help prepares metadata and the canonical conversation, but sends no
form context and starts no model. Explicit **Launch** authorizes the displayed
provider/model revision and freezes the allowlisted initial fields, draft
revision and hash before any asynchronous work. The agent consumes the canonical
form purpose, schema and frozen values through `assisted_draft_context_get`,
then sends a useful initial greeting through the ordinary conversation tool.
No user-authored message is fabricated and the user need not retype form context.

**Launch** occupies the composer's normal Send position until authorization
succeeds; there is no separate startup button or idle readiness paragraph.
**Launching…** prevents duplicate activation. An uncertain attempt offers
**Retry Launch** against its frozen disclosure, with **Launch with current
fields** as a separate explicit authorization. Launch never sends or clears an
unsent message. The same composer returns to ordinary **Send** after launch.

Subsequent authored turns keep the house message ID. Before transport, the host
stages one immutable snapshot under that ID; an uncertain acknowledgement
retains the same prepared envelope. Later manual edits cannot enlarge that
turn's disclosure and remain protected by normal patch conflicts.

A hosted agent receives only source-free binding identifiers at launch. Its
non-overridable tool set contains `assisted_draft_context_get`,
`assisted_draft_propose_patch`, and the existing send, ask, poll, receive and
acknowledge conversation tools. Every call checks the exact session,
conversation, consumer, generation, pinned model and applicable policy gates.
Initial context consumption precedes conversation access; each user turn must
be received and its exact snapshot consumed before edits or acknowledgement.
Only finite-choice/boolean questions use inline answer tools; ordinary composer
messages do not answer a pending question.

Exact content releases are Sources-backed and recorded in the shared worker
disclosure manifest before return. Output binds the ordered input manifest and
durable producer identity. A possibly-sent release is never automatically
replayed or retried under a new generation. Replies and patches have stable
identities; errors are sanitized. Unbound generic conversation routes/tools
cannot bypass the form session.

The protected HTTP flow is metadata-only `POST /api/assistance/sessions`,
GET/PATCH execution selection, explicit Launch with `initialSnapshot`, and
snapshot preparation followed by canonical conversation response. Responses to
inline questions include their exact `in_reply_to`. Stop and permanent End are
separate authority-reducing actions.

Model changes fence the old driver and return to a prepared state. Explicit
Launch is required before the new recipient receives current fields or history.
A fresh Launch supersedes unfinished prior work and old pending questions while
preserving the transcript; its frozen current snapshot is the new working base.
Start and scoped Stop compare the server-owned integer `controlRevision`.
Start checks it before and after provider validation; an exact durable Start
retry never probes or launches again. Scoped Stop carries its request identity,
expected revision and, when applicable, exact pending Start request. A delayed
Start or stale Stop retry cannot restart or cancel a successor generation.

Stop requires a fresh explicit Launch, while End permanently revokes that
binding. Cleanup remains available after opt-out, expiration, read-only or a
Source Foundation restore fence. Older session protocols require an explicit
new session; their transcript and receipts remain inspectable. Ended and expired sessions
also return a read-only recovery projection without resolving defaults or
probing providers, so a reload preserves history and conditional Undo.

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
the session preserves the form and receipts but permanently revokes its active
assistant binding on the server.

Leaving an active form host durably queues a scoped Stop, including a pending
Start, while preserving the conversation binding and unsent composer draft.
Reopening requires fresh explicit Launch. Simply closing the still-mounted dock
preserves its session. Cleanup acknowledges only a typed missing-session
response as already absent; authentication, network and other failures remain
visible and retryable.

An accepted host `draft.clear()` or `draft.reset(value, { ifRevision })`
synchronously notifies reset subscribers. Assistance invalidates its editing
generation, closes the dock, and removes the saved session binding before a
late reply can cross into the next editing lifetime. A durable local revoke
intent retries permanent server cleanup without resurrecting the old binding. Reset preserves explicitly
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

Isolated HTTP and tool tests exercise contextual startup, model selection,
exact questions, binding denial, source disclosure, idempotency, generation
fences, policy gates, cleanup, malformed patches and host receipts. React tests
exercise canonical Chat composition, preserved draft state, focus, conflicts,
conditional Undo and reset/recovery. The disposable browser host uses the real
registry, broker, Sources and conversation/form tools with deterministic
interactive provider doubles; no production tasks, jobs or external model
calls are required.
