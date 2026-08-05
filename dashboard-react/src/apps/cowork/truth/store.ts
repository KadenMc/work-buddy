import { useCallback, useRef, useSyncExternalStore } from "react";

import type { TruthClaimFilter, TruthViewScope } from "./contracts";

export type TruthComposer = "propose" | "connect" | null;

export interface TruthState {
  readonly scope: TruthViewScope;
  readonly filter: TruthClaimFilter;
  readonly selectedClaimId: string | null;
  readonly composer: TruthComposer;
}

export interface PersistedTruthState {
  readonly version: 1;
  readonly scope: TruthViewScope;
  readonly filter: TruthClaimFilter;
  readonly selectedClaimId: string | null;
}

const DEFAULT_STATE: TruthState = {
  scope: "document",
  filter: "all",
  selectedClaimId: null,
  composer: null,
};

const SCOPES = new Set<TruthViewScope>(["document", "folder"]);
const FILTERS = new Set<TruthClaimFilter>([
  "all",
  "facts",
  "proposed",
  "needs_review",
  "challenged",
  "unconnected",
]);

export const truthStateStorageKey = (
  storeId: string,
  documentId: string,
): string =>
  `wb.cowork.truth.state.v1:${encodeURIComponent(storeId)}:${encodeURIComponent(documentId)}`;

export const loadTruthState = (
  storage: Storage,
  storeId: string,
  documentId: string,
): Partial<TruthState> | null => {
  try {
    const raw = storage.getItem(truthStateStorageKey(storeId, documentId));
    if (raw === null) return null;
    const value = JSON.parse(raw) as Partial<PersistedTruthState>;
    if (
      value.version !== 1 ||
      !SCOPES.has(value.scope as TruthViewScope) ||
      !FILTERS.has(value.filter as TruthClaimFilter) ||
      !(
        value.selectedClaimId === null ||
        typeof value.selectedClaimId === "string"
      )
    ) {
      return null;
    }
    return {
      scope: value.scope,
      filter: value.filter,
      selectedClaimId: value.selectedClaimId,
      // A captured document selection is deliberately never restored.
      composer: null,
    };
  } catch {
    return null;
  }
};

export const saveTruthState = (
  storage: Storage,
  storeId: string,
  documentId: string,
  state: TruthState,
): void => {
  const payload: PersistedTruthState = {
    version: 1,
    scope: state.scope,
    filter: state.filter,
    selectedClaimId: state.selectedClaimId,
  };
  try {
    storage.setItem(
      truthStateStorageKey(storeId, documentId),
      JSON.stringify(payload),
    );
  } catch {
    // Tab-local continuity is best effort and can never break Truth browsing.
  }
};

type Listener = () => void;

export class TruthStore {
  #state: TruthState;
  readonly #listeners = new Set<Listener>();
  readonly #onChange?: (state: TruthState) => void;

  constructor(
    initial: Partial<TruthState> = {},
    options: { readonly onChange?: (state: TruthState) => void } = {},
  ) {
    this.#state = { ...DEFAULT_STATE, ...initial };
    this.#onChange = options.onChange;
  }

  readonly getState = (): TruthState => this.#state;

  readonly subscribe = (listener: Listener): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  #set(next: TruthState): void {
    if (
      next.scope === this.#state.scope &&
      next.filter === this.#state.filter &&
      next.selectedClaimId === this.#state.selectedClaimId &&
      next.composer === this.#state.composer
    ) {
      return;
    }
    this.#state = next;
    this.#onChange?.(next);
    for (const listener of this.#listeners) listener();
  }

  setScope(scope: TruthViewScope): void {
    this.#set({ ...this.#state, scope, selectedClaimId: null, composer: null });
  }

  setFilter(filter: TruthClaimFilter): void {
    this.#set({ ...this.#state, filter, selectedClaimId: null, composer: null });
  }

  selectClaim(claimId: string | null): void {
    this.#set({ ...this.#state, selectedClaimId: claimId, composer: null });
  }

  openComposer(composer: Exclude<TruthComposer, null>): void {
    this.#set({ ...this.#state, composer });
  }

  closeComposer(): void {
    this.#set({ ...this.#state, composer: null });
  }
}

/**
 * Shared-store factory for parents that need Review attention items to open
 * the same selected Truth claim. Persistence excludes ephemeral composers.
 */
export const createPersistedTruthStore = (
  storage: Storage,
  storeId: string,
  documentId: string,
): TruthStore =>
  new TruthStore(loadTruthState(storage, storeId, documentId) ?? {}, {
    onChange: (state) =>
      saveTruthState(storage, storeId, documentId, state),
  });

const strictEqual = <T,>(left: T, right: T): boolean => Object.is(left, right);

export const useTruthState = <T,>(
  store: TruthStore,
  selector: (state: TruthState) => T,
  equal: (left: T, right: T) => boolean = strictEqual,
): T => {
  const last = useRef<{ readonly value: T } | null>(null);
  const snapshot = useCallback(() => {
    const next = selector(store.getState());
    if (last.current !== null && equal(last.current.value, next)) {
      return last.current.value;
    }
    last.current = { value: next };
    return next;
  }, [equal, selector, store]);
  return useSyncExternalStore(store.subscribe, snapshot, snapshot);
};
