---
name: React Dashboard Chat Primitives
kind: concept
description: Reusable React chat components and a typed provider seam mirroring house conversation semantics for the /app dashboard.
tags:
- dashboard
- react
- chat
- conversations
- widget-library
aliases:
- ChatPanel
- chat primitives
- useChatConversation
- ChatExecutionPicker
- useChatExecutionProfile
- React chat
parents:
- services/dashboard/react
dev_notes: |-
  The provider argument to useChatConversation must be referentially stable, a fresh instance each render re-subscribes and reloads. Send results and errors that resolve after the hook rebinds to another provider or conversation are dropped by a captured-identity guard, keep that guard when extending the send path.

  The autoscroll effect keys on message COUNT, so a last message whose content grows in place will not re-stick a pinned view (the house store appends discrete messages). An unread boundary seeded at the first message is legitimate, the separator renders above index 0.

  The shared package must never import an App domain. `renderMessageAccessory` is deliberately additive rather than a whole-message renderer: canonical message content stays under house ownership, the accessory follows it, and built-in question controls remain last. `transcriptAppendix` stays inside the transcript scroller. Recovery replaces the composer and suppresses the passive stopped notice so hosts do not create conflicting controls or nested live announcements.

  `ConversationChat` keys the panel by opaque conversation id so composer state cannot leak when a mounted host changes conversations. Keep host-persisted drafts scoped at least as narrowly. The shared availability decision disables both freeform composition and structured Yes/No or choice responses while sending, read-only, stopped, explicitly disabled, or recovering; do not gate only the text composer.

  Keep transport out of `widget-library/chat`. The generic same-origin HTTP implementation lives at `dashboard-react/src/dashboard/conversations/`; App adapters inject it through the provider seam.

  Keep `ChatConversationProvider` and `ChatExecutionProfileProvider` separate. Transcript loading/sending must not acquire provider discovery, selection, revision, or agent-restart semantics. A host opts into execution controls explicitly and owns the consequence copy for a live switch.

  The execution hook has the same captured-identity rule as the conversation hook. Late loads and selections from a previous provider/target are discarded. A 409 may carry a newer authoritative snapshot; adopt it before showing the conflict.

  The HTTP adapter also fences its internal cache and `onEnvelope` callback. A successful PATCH, conflict envelope, host `replaceSnapshot`, invalidation, or explicit refresh supersedes older GETs before they can adopt; hook-level sequencing alone is too late to protect feature-owned lifecycle state.

  Selection mutations outrank subscription reloads in `useChatExecutionProfile`. A reload issued while selection is pending must not apply its stale snapshot or error; successful/authoritative mutation results clear it, while a non-authoritative mutation failure triggers one fresh post-mutation reconciliation. Execution-required host actions are unavailable whenever the snapshot is read-only, not merely when the provider/model is unavailable.
---

Reusable React chat components for conversational surfaces in the React dashboard, at `dashboard-react/src/widget-library/chat/`. They render the same backend conversations the root dashboard's chat sidebar shows, behind a typed, transport-agnostic provider seam. The root dashboard's own surface remains `services/dashboard/chat-sidebar` and is unchanged by these primitives.

## Components

- **ConversationChat**: the canonical connected conversation surface. It binds a stable provider and opaque conversation id, owns loading, retry, send errors, activity, and message observation, and composes `ChatPanel`. It may resolve a typed input-recovery state, but it knows nothing about the host App's domain.
- **ChatPanel**: message log plus composer with a header slot and the standard host states, including a read-only banner. Forwards the composer's optional `initialValue` and `onDraftChange` draft seam, so a host can retain the unsent draft across reloads.
- **ChatPanelState**: the canonical loading, empty, or error shell for a host that does not yet have a ready conversation. The host supplies state kind, direct copy, and at most one action without recreating panel markup or importing private chat styles.
- **ChatMessageList**: author attribution, timestamps, unread boundary with scroll lock and jump-to-latest, inline choice and boolean answers, typing indicator and agent-stopped notice.
- **ChatComposer**: Enter submits, Shift plus Enter inserts a newline, the draft is retained on send failure. Optional draft-observation seam: `initialValue` seeds the draft once on mount and `onDraftChange` fires on every edit (empty string after a successful send), so a host can persist the unsent draft and arm an unsaved-work guard while the composer keeps owning the text state.
- **ChatExecutionPicker**: an optional, compact **Run with** control that shows
  one atomic provider/model choice grouped by provider. Server-authored
  availability and descriptions stay visible and accessible; unavailable
  choices cannot be selected.

## Host extensions

Apps extend the canonical transcript through narrow composition seams rather
than forking it:

- `renderMessageAccessory(message)` adds an App-owned action after one
  canonical message's content and before any built-in answer controls;
- `transcriptAppendix` adds auxiliary content at the end of the scrollable
  transcript;
- typed input recovery temporarily replaces sending with one explanation and,
  when recovery is possible, one action; and
- `onMessagesChange` lets a host observe canonical messages for surrounding UI
  such as an unread tab marker.

These are React composition seams, not persisted widget input. The shared
surface continues to own message identity, ordering, author semantics,
questions, scrolling, loading, retry, sending, and accessibility. A host
cannot replace the whole message renderer. When input is unavailable, both the
composer and any structured Yes/No or choice controls are unavailable.

`ConversationChat` is a reusable surface inside dashboard Apps. It is not, by
itself, a separately placeable Dashboard Core widget. A cohesive durable App
such as Co-work may embed it while retaining one durable widget boundary.

## Optional execution profile

Model selection is reusable without becoming part of transcript transport.
`ChatExecutionProfileProvider` independently loads and changes one opaque
execution target, while `useChatExecutionProfile` owns loading, retry,
selection, optimistic-concurrency recovery, and live announcements. The
containing App passes that control to the shared Chat surface only when its
conversation has a selectable agent runtime.

The server supplies the provider/model catalog and the current validated pair.
The browser does not persist a competing default in local storage or settings.
Selection is atomic and revisioned; if another surface wins a race, the shared
hook adopts the newer server snapshot and reports that the requested switch did
not land.

The generic picker knows how to render and select a profile, but not what a
switch does. A host may provide `confirmSelection` consequence copy when an
active agent must restart. This keeps Co-work-specific restart semantics out of
the widget library while allowing other Chat hosts to make low-impact choices
immediate. A read-only execution snapshot remains inspectable but disables
selection and composition.

## State and mapping

`useChatConversation(provider, conversationId)` binds a `ChatConversationProvider` (loadConversation, sendMessage, subscribe) to React state with load, silent-refresh, and send lifecycles. `normalizeConversationPayload` converts the raw `GET /api/conversations/<id>` payload into canonical types, and `deriveAgentActivity` mirrors the legacy typing and stopped logic from `conversation.agent_alive`.

Message identity: the endpoint serializes message ids as `message_id`, which the mapping prefers. A bare `id` is accepted as a fixture-side fallback and a positional id is the last resort.

Assistant messages may include server-verified producer provenance. The shared
mapping renders its provider/model labels from the durable message, never from
the conversation's current selection, so historical attribution survives later
model changes.

## Transport posture

No HTTP wiring lives in the package. Live transports implement the conversation
and, optionally, execution-profile seams. `InMemoryChatProvider` is the
conversation test and development fixture. The generic same-origin execution
adapter lives in `dashboard-react/src/dashboard/conversations/` and normalizes
server envelopes without owning App lifecycle policy. The components consume
the appearance system's semantic tokens only, honor forced-colors and
reduced-motion with non-color encodings, and are keyboard complete.
