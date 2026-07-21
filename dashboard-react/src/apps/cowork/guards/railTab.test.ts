import { describe, expect, it } from "vitest";

import { loadRailTab, railTabStorageKey, saveRailTab } from "./railTab";

/** A minimal in-memory Storage for deterministic rail-tab tests. */
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

describe("rail tab retention", () => {
  it("round-trips a saved tab per document", () => {
    const storage = new MemoryStorage();
    saveRailTab(storage, "doc-1", "chat");
    expect(loadRailTab(storage, "doc-1")).toBe("chat");
    saveRailTab(storage, "doc-1", "review");
    expect(loadRailTab(storage, "doc-1")).toBe("review");
  });

  it("returns null for a missing key", () => {
    const storage = new MemoryStorage();
    expect(loadRailTab(storage, "doc-1")).toBeNull();
  });

  it("returns null for an unrecognized stored value", () => {
    const storage = new MemoryStorage();
    storage.setItem(railTabStorageKey("doc-1"), "sidebar");
    expect(loadRailTab(storage, "doc-1")).toBeNull();
  });

  it("keeps tabs isolated per document", () => {
    const storage = new MemoryStorage();
    saveRailTab(storage, "doc-a", "chat");
    saveRailTab(storage, "doc-b", "review");
    expect(loadRailTab(storage, "doc-a")).toBe("chat");
    expect(loadRailTab(storage, "doc-b")).toBe("review");
    expect(railTabStorageKey("doc-a")).not.toBe(railTabStorageKey("doc-b"));
  });
});
