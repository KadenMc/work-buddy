# Component inventory

Reusable components and component families in the React dashboard, one entry per public surface. Registered widget types live in the contribution registry (`src/app/dashboardRegistry.ts`) and are additionally listed in [ARCHITECTURE.md](ARCHITECTURE.md). This file inventories the reusable component layer beneath and beside them: what exists, where it lives, and what contract it exposes, so new views compose before they invent.

## Widget library (registered publishers)

| Component | Location | Contract |
|---|---|---|
| Quick Capture | `src/widget-library/capture/` | `wb.capture.quick-text` widget type, `wb.widget-role.capture@1` |
| Day Timeline | `src/widget-library/timeline/` | `wb.timeline.day` widget type, `wb.widget-role.day-timeline@1` |
| Running Notes | `src/widget-library/notes/` | `wb.notes.running` widget type, `wb.widget-role.running-notes@1` |
| Shared widget primitives | `src/widget-library/shared/` | Cross-publisher presentation helpers consumed by the library widgets |

## Chat primitives (library components, not registered widget types)

Reusable conversational surface for any view that mounts a house conversation. Knowledge unit: `services/dashboard/react/chat-primitives`.

| Component | Location | Contract |
|---|---|---|
| ChatPanel | `src/widget-library/chat/ChatPanel.tsx` | Message log plus composer with header slot and the standard host states (ready, loading, empty, error, read-only). Composer self-disables while the agent is stopped |
| ChatMessageList | `src/widget-library/chat/ChatMessageList.tsx` | Author-attributed transcript with unread boundary, scroll lock, jump-to-latest, inline choice and boolean answers, typing and agent-stopped indicators |
| ChatComposer | `src/widget-library/chat/ChatComposer.tsx` | Enter submits, Shift plus Enter newline, IME-safe, draft retained on send failure, optional `initialValue`/`onDraftChange` draft-observation seam for host-side persistence and unsaved-work guards |
| useChatConversation | `src/widget-library/chat/useChatConversation.ts` | Binds a ChatConversationProvider to load, silent-refresh, and send lifecycles. Provider must be referentially stable |
| ChatConversationProvider | `src/widget-library/chat/contracts.ts` | The transport seam: loadConversation, sendMessage, subscribe. `InMemoryChatProvider` is the test and development fixture |
| normalizeConversationPayload, deriveAgentActivity | `src/widget-library/chat/mapping.ts` | Raw `GET /api/conversations/<id>` payload to canonical types, message identity via `message_id`, legacy typing and stopped derivation |

## Adding an entry

New reusable components add a row (or a new family section) in the same landing that creates them, with the knowledge unit cross-referenced when one exists. Entries describe the current contract, never the change history.
