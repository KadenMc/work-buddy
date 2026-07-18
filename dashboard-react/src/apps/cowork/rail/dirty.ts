/**
 * Dirty-state tracking for the sitting. Batch sittings are first-class (PRD
 * section 6), so a partly-marked sitting survives a reload through localStorage
 * draft retention, and a route change while dirty is guarded. The persistence
 * and the guard are separate seams so both are testable without a router.
 */

import { useEffect } from "react";

import type { StagedClaimDecision, StagedDecision } from "./contracts";
import { isDirty } from "./store";
import type { RailStore } from "./store";

const DRAFT_VERSION = 1;

interface DraftPayload {
  readonly version: number;
  readonly decisions: Record<string, StagedDecision>;
  readonly claimDecisions: Record<string, StagedClaimDecision>;
}

/** The localStorage key for one document's sitting draft. */
export function draftStorageKey(documentId: string): string {
  return `wb.cowork.rail.draft.${documentId}`;
}

/** Read a persisted sitting draft, or null when none is present or it is stale. */
export function loadDraft(
  storage: Storage,
  documentId: string,
): Pick<DraftPayload, "decisions" | "claimDecisions"> | null {
  let raw: string | null = null;
  try {
    raw = storage.getItem(draftStorageKey(documentId));
  } catch {
    return null;
  }
  if (raw === null) return null;
  try {
    const parsed = JSON.parse(raw) as DraftPayload;
    if (parsed.version !== DRAFT_VERSION) return null;
    return {
      decisions: parsed.decisions ?? {},
      claimDecisions: parsed.claimDecisions ?? {},
    };
  } catch {
    return null;
  }
}

/** Persist the current sitting draft, or clear it when the sitting is empty. */
export function saveDraft(
  storage: Storage,
  documentId: string,
  decisions: Readonly<Record<string, StagedDecision>>,
  claimDecisions: Readonly<Record<string, StagedClaimDecision>>,
): void {
  const key = draftStorageKey(documentId);
  const empty =
    Object.keys(decisions).length === 0 &&
    Object.keys(claimDecisions).length === 0;
  try {
    if (empty) {
      storage.removeItem(key);
      return;
    }
    const payload: DraftPayload = {
      version: DRAFT_VERSION,
      decisions: { ...decisions },
      claimDecisions: { ...claimDecisions },
    };
    storage.setItem(key, JSON.stringify(payload));
  } catch {
    // A full or unavailable storage must never break the sitting.
  }
}

/** Remove one document's sitting draft (called after a successful submit). */
export function clearDraft(storage: Storage, documentId: string): void {
  try {
    storage.removeItem(draftStorageKey(documentId));
  } catch {
    // Ignore an unavailable storage.
  }
}

/**
 * Restore a persisted draft into the store on mount, then keep the draft in
 * step with the store on every change. The store is the source of truth in the
 * session, localStorage is only the reload-survival mirror.
 */
export function useDraftPersistence(
  store: RailStore,
  documentId: string,
  storage: Storage,
): void {
  useEffect(() => {
    const draft = loadDraft(storage, documentId);
    if (draft !== null) {
      store.hydrateDecisions(draft.decisions, draft.claimDecisions);
    }
    const persist = () => {
      const state = store.getState();
      saveDraft(storage, documentId, state.decisions, state.claimDecisions);
    };
    const unsubscribe = store.subscribe(persist);
    return () => {
      unsubscribe();
    };
  }, [store, documentId, storage]);
}

/**
 * The route-change guard seam. When the sitting is dirty a native beforeunload
 * prompt is armed, and the orchestrator can also read the same dirty signal to
 * wire a React Router blocker. The rail never owns the router.
 */
export function useUnsavedChangesGuard(store: RailStore, active: boolean): void {
  useEffect(() => {
    if (!active) return undefined;
    const handler = (event: BeforeUnloadEvent) => {
      if (!isDirty(store.getState())) return;
      event.preventDefault();
      // Legacy browsers require a returnValue to raise the prompt.
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => {
      window.removeEventListener("beforeunload", handler);
    };
  }, [store, active]);
}
