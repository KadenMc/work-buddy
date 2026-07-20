import { describe, expect, it, vi } from "vitest";

import {
  InMemoryCoworkYdocBackingStore,
  LocalCoworkYdocTransport,
} from "./LocalCoworkYdocTransport";
import { sha256Hex } from "./hashing";

describe("LocalCoworkYdocTransport", () => {
  it("round-trips an opaque batch across two transport instances on one key", async () => {
    // One shared backing behind both transports models the persisted store across a
    // reload: the first transport writes, a fresh second one reads the same bytes back.
    const backing = new InMemoryCoworkYdocBackingStore();
    const factory = () => backing;
    const documentId = "doc-round-trip";

    const writer = new LocalCoworkYdocTransport({ documentId, factory });
    const base = await writer.pull({});
    expect(base.snapshot).toBeNull();
    expect(base.batches).toEqual([]);

    const batch = new Uint8Array([7, 8, 9, 254]);
    const pushed = await writer.push({ batch, baseSha256: base.docSha256 });
    expect(pushed.ok).toBe(true);

    const reader = new LocalCoworkYdocTransport({ documentId, factory });
    const pulled = await reader.pull({});
    expect(pulled.batches).toEqual([batch]);
  });

  it("keeps two document ids isolated on a shared backing", async () => {
    const backing = new InMemoryCoworkYdocBackingStore();
    const factory = () => backing;

    const alpha = new LocalCoworkYdocTransport({ documentId: "alpha", factory });
    const beta = new LocalCoworkYdocTransport({ documentId: "beta", factory });

    const alphaBase = await alpha.pull({});
    await alpha.push({
      batch: new Uint8Array([1, 2, 3]),
      baseSha256: alphaBase.docSha256,
    });

    // The other document id sees none of alpha's work.
    const betaPull = await beta.pull({});
    expect(betaPull.snapshot).toBeNull();
    expect(betaPull.batches).toEqual([]);

    // And alpha still holds exactly its own write.
    const alphaPull = await alpha.pull({});
    expect(alphaPull.batches).toEqual([new Uint8Array([1, 2, 3])]);
  });

  it("stores a compaction snapshot and truncates the superseded log", async () => {
    const transport = new LocalCoworkYdocTransport({
      documentId: "doc-compaction",
      factory: () => new InMemoryCoworkYdocBackingStore(),
    });
    const base = await transport.pull({});
    await transport.push({ batch: new Uint8Array([1]), baseSha256: base.docSha256 });

    const afterEdit = await transport.pull({});
    expect(afterEdit.batches).toHaveLength(1);

    const snapshot = new Uint8Array([9, 9, 9, 9]);
    const snapshotSha256 = await sha256Hex(snapshot);
    const compacted = await transport.push({
      batch: new Uint8Array([2]),
      baseSha256: afterEdit.docSha256,
      compaction: { snapshot, snapshotSha256 },
    });
    expect(compacted.ok).toBe(true);

    const pulled = await transport.pull({});
    expect(pulled.snapshot).toEqual(snapshot);
    expect(pulled.snapshotSha256).toBe(snapshotSha256);
    expect(pulled.batches).toEqual([]);
    // The offset advanced past both entries the snapshot subsumed.
    expect(pulled.nextOffset).toBe("2");
  });

  it("rejects a push whose base hash does not match the stored state", async () => {
    const transport = new LocalCoworkYdocTransport({
      documentId: "doc-stale",
      factory: () => new InMemoryCoworkYdocBackingStore(),
    });
    const result = await transport.push({
      batch: new Uint8Array([0]),
      baseSha256: "not-the-current-hash",
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toBe("stale_base");
      // The rejection reports the real current fingerprint so the client can rebase.
      const current = await transport.pull({});
      expect(result.serverDocSha256).toBe(current.docSha256);
    }
  });

  it("falls back to a process-memory backing when indexedDB is undefined", async () => {
    vi.stubGlobal("indexedDB", undefined);
    try {
      // No factory, so the default path chooses the fallback rather than throwing.
      const transport = new LocalCoworkYdocTransport({ documentId: "doc-fallback" });
      const base = await transport.pull({});
      expect(base.snapshot).toBeNull();
      expect(base.batches).toEqual([]);

      const batch = new Uint8Array([42, 43]);
      const pushed = await transport.push({ batch, baseSha256: base.docSha256 });
      expect(pushed.ok).toBe(true);

      const pulled = await transport.pull({});
      expect(pulled.batches).toEqual([batch]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("slices the append log by offset and omits the snapshot", async () => {
    const transport = new LocalCoworkYdocTransport({
      documentId: "doc-slice",
      factory: () => new InMemoryCoworkYdocBackingStore(),
    });
    const base = await transport.pull({});
    await transport.push({ batch: new Uint8Array([10]), baseSha256: base.docSha256 });
    const afterFirst = await transport.pull({});
    expect(afterFirst.batches).toHaveLength(1);

    await transport.push({
      batch: new Uint8Array([20]),
      baseSha256: afterFirst.docSha256,
    });

    const slice = await transport.pull({ sinceOffset: afterFirst.nextOffset });
    expect(slice.snapshot).toBeNull();
    expect(slice.batches).toEqual([new Uint8Array([20])]);
  });

  it("rejects a compaction blob that does not re-hash to its declared digest", async () => {
    const transport = new LocalCoworkYdocTransport({
      documentId: "doc-rehash",
      factory: () => new InMemoryCoworkYdocBackingStore(),
    });
    const base = await transport.pull({});
    await expect(
      transport.push({
        batch: new Uint8Array([1]),
        baseSha256: base.docSha256,
        compaction: { snapshot: new Uint8Array([1, 2, 3]), snapshotSha256: "0000" },
      }),
    ).rejects.toThrow(/re-hash/);
  });
});
