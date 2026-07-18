import { describe, expect, it } from "vitest";

import {
  chatDraftStorageKey,
  clearChatDraft,
  isChatDraftDirty,
  loadChatDraft,
  saveChatDraft,
} from "./chatDraft";

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

describe("isChatDraftDirty", () => {
  it("treats a non-empty trimmed body as dirty", () => {
    expect(isChatDraftDirty("hello")).toBe(true);
    expect(isChatDraftDirty("   ")).toBe(false);
    expect(isChatDraftDirty("")).toBe(false);
  });
});

describe("chat draft retention", () => {
  it("round-trips a saved draft per conversation", () => {
    const storage = new MemoryStorage();
    saveChatDraft(storage, "conv-1", "half a message");
    expect(loadChatDraft(storage, "conv-1")).toBe("half a message");
    expect(loadChatDraft(storage, "conv-2")).toBeNull();
  });

  it("clears the draft when it empties", () => {
    const storage = new MemoryStorage();
    saveChatDraft(storage, "conv-1", "typing");
    saveChatDraft(storage, "conv-1", "   ");
    expect(loadChatDraft(storage, "conv-1")).toBeNull();
    expect(storage.getItem(chatDraftStorageKey("conv-1"))).toBeNull();
  });

  it("clears a draft explicitly after a send", () => {
    const storage = new MemoryStorage();
    saveChatDraft(storage, "conv-1", "sent soon");
    clearChatDraft(storage, "conv-1");
    expect(loadChatDraft(storage, "conv-1")).toBeNull();
  });

  it("keys drafts distinctly per conversation", () => {
    expect(chatDraftStorageKey("a")).not.toBe(chatDraftStorageKey("b"));
  });
});
