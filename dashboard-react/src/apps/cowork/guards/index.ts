/**
 * Co-work route-change dirty guards (PRD sections 6 and 7). The surface composes the mark
 * sitting dirty signal (rail/dirty.ts) and the unsent chat draft (chatDraft.ts) into one
 * guard, arming a beforeunload prompt for browser-level route changes and a confirm seam for
 * programmatic navigation and the coarse document switch.
 */

export {
  chatDraftStorageKey,
  clearChatDraft,
  isChatDraftDirty,
  loadChatDraft,
  saveChatDraft,
  useChatDraftPersistence,
} from "./chatDraft";
export { loadRailTab, saveRailTab } from "./railTab";
export {
  UNSAVED_WORK_PROMPT,
  anyDirty,
  confirmDiscardUnsavedWork,
  guardedNavigate,
  useUnsavedWorkGuard,
  type ConfirmDiscardOptions,
} from "./routeGuard";
