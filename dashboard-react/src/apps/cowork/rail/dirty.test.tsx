import { render, act } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  clearDraft,
  draftStorageKey,
  loadDraft,
  saveDraft,
  useDraftPersistence,
  useUnsavedChangesGuard,
} from "./dirty";
import { RailStore, isDirty } from "./store";

/** A minimal in-memory Storage for deterministic draft tests. */
class MemoryStorage implements Storage {
  private map = new Map<string, string>();
  get length(): number {
    return this.map.size;
  }
  clear(): void {
    this.map.clear();
  }
  getItem(key: string): string | null {
    return this.map.get(key) ?? null;
  }
  key(index: number): string | null {
    return [...this.map.keys()][index] ?? null;
  }
  removeItem(key: string): void {
    this.map.delete(key);
  }
  setItem(key: string, value: string): void {
    this.map.set(key, value);
  }
}

const decision = {
  proposalId: "p1",
  verb: "confirm" as const,
  canonicalSha256: "hash",
};

describe("draft persistence functions", () => {
  it("round-trips a saved draft", () => {
    const storage = new MemoryStorage();
    saveDraft(storage, "doc", { p1: decision }, {});
    const loaded = loadDraft(storage, "doc");
    expect(loaded?.decisions.p1.verb).toBe("confirm");
  });

  it("removes the draft when the sitting is empty", () => {
    const storage = new MemoryStorage();
    saveDraft(storage, "doc", { p1: decision }, {});
    saveDraft(storage, "doc", {}, {});
    expect(storage.getItem(draftStorageKey("doc"))).toBeNull();
  });

  it("rejects a draft written under a different version", () => {
    const storage = new MemoryStorage();
    storage.setItem(
      draftStorageKey("doc"),
      JSON.stringify({ version: 99, decisions: {}, claimDecisions: {} }),
    );
    expect(loadDraft(storage, "doc")).toBeNull();
  });

  it("clears a draft explicitly", () => {
    const storage = new MemoryStorage();
    saveDraft(storage, "doc", { p1: decision }, {});
    clearDraft(storage, "doc");
    expect(loadDraft(storage, "doc")).toBeNull();
  });
});

function PersistHarness({
  store,
  storage,
}: {
  store: RailStore;
  storage: Storage;
}) {
  useDraftPersistence(store, "doc", storage);
  return null;
}

describe("useDraftPersistence", () => {
  it("hydrates a persisted draft into the store on mount", () => {
    const storage = new MemoryStorage();
    saveDraft(storage, "doc", { p1: decision }, {});
    const store = new RailStore();
    render(<PersistHarness store={store} storage={storage} />);
    expect(store.getState().decisions.p1.verb).toBe("confirm");
  });

  it("mirrors a staged decision into storage", () => {
    const storage = new MemoryStorage();
    const store = new RailStore();
    render(<PersistHarness store={store} storage={storage} />);
    act(() => {
      store.stageDecision(decision);
    });
    expect(loadDraft(storage, "doc")?.decisions.p1.verb).toBe("confirm");
  });
});

function GuardHarness({ store }: { store: RailStore }) {
  useUnsavedChangesGuard(store, true);
  return null;
}

describe("useUnsavedChangesGuard", () => {
  it("prevents unload only while the sitting is dirty", () => {
    const store = new RailStore();
    render(<GuardHarness store={store} />);

    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);

    act(() => {
      store.stageDecision(decision);
    });
    expect(isDirty(store.getState())).toBe(true);

    const dirty = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
  });
});
