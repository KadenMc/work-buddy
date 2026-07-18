/**
 * Unsent chat-draft retention for the Co-work Chat tab. The mark sitting already survives a
 * reload through rail/dirty.ts localStorage retention (PRD section 6). An unsent message in
 * the document conversation is the second kind of unsaved work the route-change guard must
 * account for (PRD section 7 feedback walkthrough), so it retains the same way: one draft per
 * conversation, mirrored to localStorage, cleared when sent or emptied. Kept independent of
 * the rail store so both dirty sources compose without either owning the other.
 */

import { useEffect } from "react";

const DRAFT_PREFIX = "wb.cowork.chat.draft.";

/** The localStorage key for one conversation's unsent draft. */
export function chatDraftStorageKey(conversationId: string): string {
  return `${DRAFT_PREFIX}${conversationId}`;
}

/** Whether a draft body counts as unsaved work (non-empty after trimming). */
export function isChatDraftDirty(text: string): boolean {
  return text.trim().length > 0;
}

/** Read a retained chat draft, or null when none is present. */
export function loadChatDraft(
  storage: Storage,
  conversationId: string,
): string | null {
  try {
    return storage.getItem(chatDraftStorageKey(conversationId));
  } catch {
    return null;
  }
}

/** Persist the current chat draft, or clear it when the draft is empty. */
export function saveChatDraft(
  storage: Storage,
  conversationId: string,
  text: string,
): void {
  const key = chatDraftStorageKey(conversationId);
  try {
    if (isChatDraftDirty(text)) {
      storage.setItem(key, text);
    } else {
      storage.removeItem(key);
    }
  } catch {
    // A full or unavailable storage must never break the chat input.
  }
}

/** Remove one conversation's retained draft (called after a successful send). */
export function clearChatDraft(storage: Storage, conversationId: string): void {
  try {
    storage.removeItem(chatDraftStorageKey(conversationId));
  } catch {
    // Ignore an unavailable storage.
  }
}

/**
 * Keep the retained draft in step with the live chat input. The input owns the text state,
 * localStorage is only the reload-survival mirror, so this persists on every change and
 * clears when the draft empties. The consumer seeds its input from loadChatDraft on mount.
 */
export function useChatDraftPersistence(
  storage: Storage,
  conversationId: string,
  text: string,
): void {
  useEffect(() => {
    saveChatDraft(storage, conversationId, text);
  }, [storage, conversationId, text]);
}
