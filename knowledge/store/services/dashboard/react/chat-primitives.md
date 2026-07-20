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

  The composer disables itself while agentActivity is stopped, callers do not need to derive that. Inline question answers thread the question's message id as onSend's second argument and catch rejection, failures surface through sendErrorMessage from the container.
---

Reusable React chat components for conversational surfaces in the React dashboard, at `dashboard-react/src/widget-library/chat/`. They render the same backend conversations the root dashboard's chat sidebar shows, behind a typed, transport-agnostic provider seam. The root dashboard's own surface remains `services/dashboard/chat-sidebar` and is unchanged by these primitives.

## Components

- **ChatPanel**: message log plus composer with a header slot and the standard host states, including a read-only banner. Forwards the composer's optional `initialValue` and `onDraftChange` draft seam, so a host can retain the unsent draft across reloads.
- **ChatMessageList**: author attribution, timestamps, unread boundary with scroll lock and jump-to-latest, inline choice and boolean answers, typing indicator and agent-stopped notice.
- **ChatComposer**: Enter submits, Shift plus Enter inserts a newline, the draft is retained on send failure. Optional draft-observation seam: `initialValue` seeds the draft once on mount and `onDraftChange` fires on every edit (empty string after a successful send), so a host can persist the unsent draft and arm an unsaved-work guard while the composer keeps owning the text state.

## State and mapping

`useChatConversation(provider, conversationId)` binds a `ChatConversationProvider` (loadConversation, sendMessage, subscribe) to React state with load, silent-refresh, and send lifecycles. `normalizeConversationPayload` converts the raw `GET /api/conversations/<id>` payload into canonical types, and `deriveAgentActivity` mirrors the legacy typing and stopped logic from `conversation.agent_alive`.

Message identity: the endpoint serializes message ids as `message_id`, which the mapping prefers. A bare `id` is accepted as a fixture-side fallback and a positional id is the last resort.

## Transport posture

No HTTP wiring lives in the package. A live transport implements the provider seam. `InMemoryChatProvider` is the test and development fixture. The components consume the appearance system's semantic tokens only, honor forced-colors and reduced-motion with non-color encodings, and are keyboard complete.
