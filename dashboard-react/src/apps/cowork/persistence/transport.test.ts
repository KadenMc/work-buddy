import { describe, expect, it, vi } from "vitest";

import { InMemoryCoworkYdocTransport } from "./InMemoryCoworkYdocTransport";
import { sha256Hex } from "./hashing";
import { structuredHeadSha256 } from "./structuredHead";

describe("InMemoryCoworkYdocTransport", () => {
  it("pushes an opaque batch and pulls it back byte-for-byte", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const initial = await transport.pull({});
    expect(initial.snapshot).toBeNull();
    expect(initial.batches).toEqual([]);

    const batch = new Uint8Array([1, 2, 3, 250]);
    const result = await transport.push({
      batch,
      baseSha256: initial.docSha256,
      baseYdocGeneration: initial.ydocGeneration,
    });
    expect(result.ok).toBe(true);

    const pulled = await transport.pull({});
    expect(pulled.batches).toEqual([batch]);
  });

  it("rejects a push whose base hash does not match the server state", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const current = await transport.pull({});
    const result = await transport.push({
      batch: new Uint8Array([0]),
      baseSha256: "not-the-current-hash",
      baseYdocGeneration: current.ydocGeneration,
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toBe("stale_base");
      expect(result.serverDocSha256).toBe(transport.docSha256);
    }
  });

  it("rejects a push from another logical Y.Doc generation", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const current = await transport.pull({});

    const result = await transport.push({
      batch: new Uint8Array([1, 2, 3]),
      baseSha256: current.docSha256,
      baseYdocGeneration: "cowork-ydoc-generation/v1:other",
    });

    expect(result).toMatchObject({
      ok: false,
      error: "stale_base",
      serverYdocGeneration: current.ydocGeneration,
    });
    expect((await transport.pull({})).batches).toEqual([]);
  });

  it("slices the append log by offset and omits the snapshot", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const base = await transport.pull({});
    await transport.push({
      batch: new Uint8Array([10]),
      baseSha256: base.docSha256,
      baseYdocGeneration: base.ydocGeneration,
    });
    const afterFirst = await transport.pull({});
    expect(afterFirst.batches).toHaveLength(1);

    await transport.push({
      batch: new Uint8Array([20]),
      baseSha256: afterFirst.docSha256,
      baseYdocGeneration: afterFirst.ydocGeneration,
    });

    const slice = await transport.pull({ sinceOffset: afterFirst.nextOffset });
    expect(slice.snapshot).toBeNull();
    expect(slice.batches).toEqual([new Uint8Array([20])]);
  });

  it("stores a compaction snapshot and truncates the superseded log", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const base = await transport.pull({});
    await transport.push({
      batch: new Uint8Array([1]),
      baseSha256: base.docSha256,
      baseYdocGeneration: base.ydocGeneration,
    });

    const afterEdit = await transport.pull({});
    const snapshot = new Uint8Array([9, 9, 9, 9]);
    const snapshotSha256 = await sha256Hex(snapshot);
    const compacted = await transport.push({
      batch: new Uint8Array([2]),
      baseSha256: afterEdit.docSha256,
      baseYdocGeneration: afterEdit.ydocGeneration,
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
        baseYdocGeneration: base.ydocGeneration,
        compaction: {
          snapshot: new Uint8Array([1, 2, 3]),
          snapshotSha256: "0000",
        },
      }),
    ).rejects.toThrow(/re-hash/);
    // The rejected request does not hold up the next one.
    expect((await transport.pull({})).batches).toEqual([]);
  });

  it("serves a caller behind the snapshot boundary a full pull", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const base = await transport.pull({});
    const staleOffset = base.nextOffset;
    await transport.push({
      batch: new Uint8Array([1]),
      baseSha256: base.docSha256,
      baseYdocGeneration: base.ydocGeneration,
    });

    const afterEdit = await transport.pull({});
    const snapshot = new Uint8Array([5, 5]);
    const snapshotSha256 = await sha256Hex(snapshot);
    await transport.push({
      batch: new Uint8Array([2]),
      baseSha256: afterEdit.docSha256,
      baseYdocGeneration: afterEdit.ydocGeneration,
      compaction: { snapshot, snapshotSha256 },
    });

    // The caller's old offset now predates the snapshot boundary, so it gets a full pull.
    const pulled = await transport.pull({ sinceOffset: staleOffset });
    expect(pulled.snapshot).toEqual(snapshot);
  });

  it("applies overlapping pushes from one base one at a time", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    const base = await transport.pull({});
    const push = (byte: number) =>
      transport.push({
        batch: new Uint8Array([byte]),
        baseSha256: base.docSha256,
        baseYdocGeneration: base.ydocGeneration,
      });

    const [first, second] = await Promise.all([push(1), push(2)]);

    expect(first.ok).toBe(true);
    expect(second).toMatchObject({ ok: false, error: "stale_base" });
    expect((await transport.pull({})).batches).toEqual([new Uint8Array([1])]);
  });

  it("keeps its fingerprint on the stored blobs when hashes settle out of order", async () => {
    // Deliver each digest only when released, newest first, so wherever two hashes are in
    // flight together the older one settles last.
    const subtle = globalThis.crypto.subtle;
    const digest = subtle.digest.bind(subtle);
    const held: { release: () => void; settled: Promise<ArrayBuffer> }[] = [];
    const spy = vi.spyOn(subtle, "digest").mockImplementation((algorithm, data) => {
      const settled = digest(algorithm, data);
      return new Promise<ArrayBuffer>((resolve, reject) => {
        held.push({ release: () => void settled.then(resolve, reject), settled });
      });
    });
    const releaseNewest = async (): Promise<boolean> => {
      const newest = held.pop();
      if (newest === undefined) return false;
      newest.release();
      await newest.settled;
      return true;
    };
    const nextTurn = () => new Promise((resolve) => setTimeout(resolve, 0));
    const batch = new Uint8Array([7]);
    const transport = new InMemoryCoworkYdocTransport();
    try {
      let finished = false;
      const markFinished = () => {
        finished = true;
      };
      const pushed = transport.pull({}).then((base) =>
        transport.push({
          batch,
          baseSha256: base.docSha256,
          baseYdocGeneration: base.ydocGeneration,
        }),
      );
      void pushed.then(markFinished, markFinished);
      await nextTurn();
      while (!finished) {
        if (!(await releaseNewest())) throw new Error("A request stalled without a hash");
        await nextTurn();
      }
      // Any hash still in flight now lands after the requests have finished.
      while (held.length > 0) await releaseNewest();
      await nextTurn();
      expect((await pushed).ok).toBe(true);
    } finally {
      spy.mockRestore();
    }

    expect((await transport.pull({})).docSha256).toBe(
      await structuredHeadSha256(new Uint8Array(0), [batch]),
    );
  });
});
