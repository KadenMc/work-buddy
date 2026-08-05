import { describe, expect, it } from "vitest";

import {
  createPersistedTruthStore,
  loadTruthState,
  saveTruthState,
  TruthStore,
  truthStateStorageKey,
} from "./store";

class MemoryStorage implements Storage {
  readonly #values = new Map<string, string>();
  get length(): number { return this.#values.size; }
  clear(): void { this.#values.clear(); }
  getItem(key: string): string | null { return this.#values.get(key) ?? null; }
  key(index: number): string | null { return [...this.#values.keys()][index] ?? null; }
  removeItem(key: string): void { this.#values.delete(key); }
  setItem(key: string, value: string): void { this.#values.set(key, value); }
}

describe("TruthStore", () => {
  it("persists the observational view and selected claim, but never a selection composer", () => {
    const storage = new MemoryStorage();
    const store = createPersistedTruthStore(storage, "store/one", "doc/one");

    store.setScope("folder");
    store.setFilter("needs_review");
    store.selectClaim("claim-1");
    store.openComposer("connect");

    expect(loadTruthState(storage, "store/one", "doc/one")).toEqual({
      scope: "folder",
      filter: "needs_review",
      selectedClaimId: "claim-1",
      composer: null,
    });
    expect(truthStateStorageKey("store/one", "doc/one")).toContain("store%2Fone");
  });

  it("ignores invalid persisted state instead of breaking Truth", () => {
    const storage = new MemoryStorage();
    storage.setItem(truthStateStorageKey("store", "doc"), "not json");
    expect(loadTruthState(storage, "store", "doc")).toBeNull();

    saveTruthState(storage, "store", "doc", new TruthStore().getState());
    expect(loadTruthState(storage, "store", "doc")).toMatchObject({
      scope: "document",
      filter: "all",
    });
  });
});
