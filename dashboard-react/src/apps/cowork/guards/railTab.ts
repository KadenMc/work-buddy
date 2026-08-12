/**
 * Rail-tab retention for the Co-work rail. The Review | Truth | Chat selection is
 * widget-local state the durable widget owns, the same kind of reload-surviving local state the
 * mark sitting (rail/dirty.ts) and the unsent chat draft (chatDraft.ts) already retain. It
 * mirrors to localStorage the same way, keyed by full Folder/document identity. The stored
 * value is validated back to a real RailTab on read, so a stale or hand-edited key falls back to
 * the default rather than pushing an unknown tab into the rail. Kept independent of the rail
 * store so the store stays storage-agnostic and the seed site owns the persistence wiring.
 */

import type { RailTab } from "../rail/store";

const TAB_PREFIX = "wb.cowork.rail.tab.";

/** The localStorage key for one document's rail tab. */
export function railTabStorageKey(
  documentId: string,
  storeId?: string,
): string {
  return storeId === undefined || storeId.length === 0
    ? `${TAB_PREFIX}${documentId}`
    : `${TAB_PREFIX}store:${encodeURIComponent(storeId)}:document:${encodeURIComponent(documentId)}`;
}

/** Whether a stored value is a real RailTab and safe to restore. */
function isRailTab(value: string | null): value is RailTab {
  return (
    value === "review" ||
    value === "truth" ||
    value === "provenance" ||
    value === "chat"
  );
}

/** Read a retained rail tab, or null when none is present or it is unrecognized. */
export function loadRailTab(
  storage: Storage,
  documentId: string,
  storeId?: string,
): RailTab | null {
  let raw: string | null = null;
  try {
    raw = storage.getItem(railTabStorageKey(documentId, storeId));
    // One-way compatibility for tabs saved before Folder identity became part
    // of the key. New writes remain collision-safe and never update this key.
    if (raw === null && storeId !== undefined && storeId.length > 0) {
      raw = storage.getItem(railTabStorageKey(documentId));
    }
  } catch {
    return null;
  }
  return isRailTab(raw) ? raw : null;
}

/** Persist the current rail tab for this document. */
export function saveRailTab(
  storage: Storage,
  documentId: string,
  tab: RailTab,
  storeId?: string,
): void {
  try {
    storage.setItem(railTabStorageKey(documentId, storeId), tab);
  } catch {
    // A full or unavailable storage must never break the rail.
  }
}
