import { describe, expect, it } from "vitest";

import { InMemoryCoworkYdocTransport } from "./InMemoryCoworkYdocTransport";
import { sha256Hex } from "./hashing";

describe("InMemoryCoworkYdocTransport", () => {
  it("pushes an opaque batch and pulls it back byte-for-byte", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const initial = await transport.pull({});
    expect(initial.snapshot).toBeNull();
    expect(initial.batches).toEqual([]);

    const batch = new Uint8Array([1, 2, 3, 250]);
    const result = await transport.push({ batch, baseSha256: initial.docSha256 });
    expect(result.ok).toBe(true);

    const pulled = await transport.pull({});
    expect(pulled.batches).toEqual([batch]);
  });

  it("rejects a push whose base hash does not match the server state", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const result = await transport.push({
      batch: new Uint8Array([0]),
      baseSha256: "not-the-current-hash",
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toBe("stale_base");
      expect(result.serverDocSha256).toBe(transport.docSha256);
    }
  });

  it("slices the append log by offset and omits the snapshot", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const base = await transport.pull({});
    await transport.push({
      batch: new Uint8Array([10]),
      baseSha256: base.docSha256,
    });
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

  it("stores a compaction snapshot and truncates the superseded log", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const base = await transport.pull({});
    await transport.push({ batch: new Uint8Array([1]), baseSha256: base.docSha256 });

    const afterEdit = await transport.pull({});
    const snapshot = new Uint8Array([9, 9, 9, 9]);
    const snapshotSha256 = await sha256Hex(snapshot);
    const compacted = await transport.push({
      batch: new Uint8Array([2]),
      baseSha256: afterEdit.docSha256,
      compaction: { snapshot, snapshotSha256 },
    });
    expect(compacted.ok).toBe(true);
    expect(transport.pendingBatchCount).toBe(0);
    expect(transport.hasSnapshot).toBe(true);

    const pulled = await transport.pull({});
    expect(pulled.snapshot).toEqual(snapshot);
    expect(pulled.snapshotSha256).toBe(snapshotSha256);
    expect(pulled.batches).toEqual([]);
  });

  it("rejects a compaction blob that does not re-hash to its declared digest", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const base = await transport.pull({});
    await expect(
      transport.push({
        batch: new Uint8Array([1]),
        baseSha256: base.docSha256,
        compaction: {
          snapshot: new Uint8Array([1, 2, 3]),
          snapshotSha256: "0000",
        },
      }),
    ).rejects.toThrow(/re-hash/);
  });

  it("serves a caller behind the snapshot boundary a full pull", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const base = await transport.pull({});
    const staleOffset = base.nextOffset;
    await transport.push({ batch: new Uint8Array([1]), baseSha256: base.docSha256 });

    const afterEdit = await transport.pull({});
    const snapshot = new Uint8Array([5, 5]);
    const snapshotSha256 = await sha256Hex(snapshot);
    await transport.push({
      batch: new Uint8Array([2]),
      baseSha256: afterEdit.docSha256,
      compaction: { snapshot, snapshotSha256 },
    });

    // The caller's old offset now predates the snapshot boundary, so it gets a full pull.
    const pulled = await transport.pull({ sinceOffset: staleOffset });
    expect(pulled.snapshot).toEqual(snapshot);
  });
});
