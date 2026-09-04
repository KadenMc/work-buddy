# Component inventory

Reusable components and component families in the React dashboard, one entry per public surface. Registered widget types live in the contribution registry (`src/app/dashboardRegistry.ts`) and are additionally listed in [ARCHITECTURE.md](ARCHITECTURE.md). This file inventories the reusable component layer beneath and beside them: what exists, where it lives, and what contract it exposes, so new views compose before they invent.

## Widget library (registered publishers)

| Component | Location | Contract |
|---|---|---|
| Quick Capture | `src/widget-library/capture/` | `wb.capture.quick-text` widget type, `wb.widget-role.capture@1` |
| Capture follow-ups | `src/widget-library/capture/FollowUpLinks.tsx` | Generic same-origin App links and pending/failed status shared by Quick Capture and Running Notes; domain providers validate reference/query identity. See `journal/source-backed-capture` |
| Day Timeline | `src/widget-library/timeline/` | `wb.timeline.day` widget type, `wb.widget-role.day-timeline@1` |
| Running Notes | `src/widget-library/notes/` | `wb.notes.running` widget type, `wb.widget-role.running-notes@1` |
| Shared widget primitives | `src/widget-library/shared/` | Cross-publisher presentation helpers consumed by the library widgets |

## Shared UI primitives: busy states

Foundation-consuming presentation components in `src/ui/`, imported through the barrel. They are the compatibility boundary between built-in and contributed UI, so views compose them rather than restyling host markup, and their styles live in `src/theme/components.css` under `@layer wb.components`. Knowledge unit: `services/dashboard/react/appearance`. The rows below cover the busy-state primitives; the rest of `src/ui/` is not yet inventoried here.

| Component | Location | Contract |
|---|---|---|
| ActivityStatus | `src/ui/ActivityStatus.tsx` | Busy state with an optional running quantity. `role="status"` scopes the announcement to `label`; `detail` is a secondary line for work whose total is unknown, rendered as a sibling outside that region and `aria-hidden`, so a rising count stays a sighted-user signal rather than re-announcing through the implicit polite live region. Indeterminate by construction, so no `role="progressbar"`. Callers mark the surrounding region `aria-busy` |

## Chat primitives (library components, not registered widget types)

Reusable conversational surface for any view that mounts a house conversation. Knowledge unit: `services/dashboard/react/chat-primitives`.

| Component | Location | Contract |
|---|---|---|
| ChatPanel | `src/widget-library/chat/ChatPanel.tsx` | Message log plus composer with header slot and the standard host states (ready, loading, empty, error, read-only). Composer self-disables while the agent is stopped. Forwards the optional `initialValue`/`onDraftChange` draft seam to the composer for host-side draft retention |
| ChatMessageList | `src/widget-library/chat/ChatMessageList.tsx` | Author-attributed transcript with unread boundary, scroll lock, jump-to-latest, inline choice and boolean answers, typing and agent-stopped indicators |
| ChatComposer | `src/widget-library/chat/ChatComposer.tsx` | Enter submits, Shift plus Enter newline, IME-safe, draft retained on send failure, optional `initialValue`/`onDraftChange` draft-observation seam for host-side persistence and unsaved-work guards |
| useChatConversation | `src/widget-library/chat/useChatConversation.ts` | Binds a ChatConversationProvider to load, silent-refresh, and send lifecycles. Provider must be referentially stable |
| ChatConversationProvider | `src/widget-library/chat/contracts.ts` | The transport seam: loadConversation, sendMessage, subscribe. `InMemoryChatProvider` is the test and development fixture |
| normalizeConversationPayload, deriveAgentActivity | `src/widget-library/chat/mapping.ts` | Raw `GET /api/conversations/<id>` payload to canonical types, message identity via `message_id`, legacy typing and stopped derivation |

## Host-owned form assistance

| Component | Location | Contract |
|---|---|---|
| AssistedDraftRuntimeProvider, useAssistedDraft, AssistDraftButton | `src/dashboard/assistance/` | Opt-in assistance for declared widget drafts, using the existing `ConversationChat` surface; typed field patches, focused-field suggestions, revision fencing, persisted receipts, conditional Undo, and reset-generation cancellation. Not a placeable widget. See `services/dashboard/react/assisted-drafts` |
| TaskDraftFields | `src/apps/tasks/composer/TaskDraftFields.tsx` | One App-owned field renderer shared by Quick Add and durable proposal review; supports focus/assistance markers without owning submission |

## Adding an entry

New reusable components add a row (or a new family section) in the same landing that creates them, with the knowledge unit cross-referenced when one exists. Entries describe the current contract, never the change history.
