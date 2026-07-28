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
- React chat
parents:
- services/dashboard/react
dev_notes: |-
  The provider argument to useChatConversation must be referentially stable, a fresh instance each render re-subscribes and reloads. Send results and errors that resolve after the hook rebinds to another provider or conversation are dropped by a captured-identity guard, keep that guard when extending the send path.

  The autoscroll effect keys on message COUNT, so a last message whose content grows in place will not re-stick a pinned view (the house store appends discrete messages). An unread boundary seeded at the first message is legitimate, the separator renders above index 0.

  The shared package must never import an App domain. `renderMessageAccessory` is deliberately additive rather than a whole-message renderer: canonical message content stays under house ownership, the accessory follows it, and built-in question controls remain last. `transcriptAppendix` stays inside the transcript scroller. Recovery replaces the composer and suppresses the passive stopped notice so hosts do not create conflicting controls or nested live announcements.

  `ConversationChat` keys the panel by opaque conversation id so composer state cannot leak when a mounted host changes conversations. Keep host-persisted drafts scoped at least as narrowly. The shared availability decision disables both freeform composition and structured Yes/No or choice responses while sending, read-only, stopped, explicitly disabled, or recovering; do not gate only the text composer.

  Keep transport out of `widget-library/chat`. The generic same-origin HTTP implementation lives at `dashboard-react/src/dashboard/conversations/`; App adapters inject it through the provider seam.
---

Reusable React chat components for conversational surfaces in the React dashboard, at `dashboard-react/src/widget-library/chat/`. They render the same backend conversations the root dashboard's chat sidebar shows, behind a typed, transport-agnostic provider seam. The root dashboard's own surface remains `services/dashboard/chat-sidebar` and is unchanged by these primitives.

## Components

- **ConversationChat**: the canonical connected conversation surface. It binds a stable provider and opaque conversation id, owns loading, retry, send errors, activity, and message observation, and composes `ChatPanel`. It may resolve a typed input-recovery state, but it knows nothing about the host App's domain.
- **ChatPanel**: message log plus composer with a header slot and the standard host states, including a read-only banner. Forwards the composer's optional `initialValue` and `onDraftChange` draft seam, so a host can retain the unsent draft across reloads.
- **ChatPanelState**: the canonical loading, empty, or error shell for a host that does not yet have a ready conversation. The host supplies state kind, direct copy, and at most one action without recreating panel markup or importing private chat styles.
- **ChatMessageList**: author attribution, timestamps, unread boundary with scroll lock and jump-to-latest, inline choice and boolean answers, typing indicator and agent-stopped notice.
- **ChatComposer**: Enter submits, Shift plus Enter inserts a newline, the draft is retained on send failure. Optional draft-observation seam: `initialValue` seeds the draft once on mount and `onDraftChange` fires on every edit (empty string after a successful send), so a host can persist the unsent draft and arm an unsaved-work guard while the composer keeps owning the text state.

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

## State and mapping

`useChatConversation(provider, conversationId)` binds a `ChatConversationProvider` (loadConversation, sendMessage, subscribe) to React state with load, silent-refresh, and send lifecycles. `normalizeConversationPayload` converts the raw `GET /api/conversations/<id>` payload into canonical types, and `deriveAgentActivity` mirrors the legacy typing and stopped logic from `conversation.agent_alive`.

Message identity: the endpoint serializes message ids as `message_id`, which the mapping prefers. A bare `id` is accepted as a fixture-side fallback and a positional id is the last resort.

## Transport posture

No HTTP wiring lives in the package. A live transport implements the provider seam. `InMemoryChatProvider` is the test and development fixture. The components consume the appearance system's semantic tokens only, honor forced-colors and reduced-motion with non-color encodings, and are keyboard complete.
