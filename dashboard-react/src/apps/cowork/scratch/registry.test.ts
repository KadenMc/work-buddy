import { beforeEach, describe, expect, it } from "vitest";

import {
  InMemoryCoworkYdocBackingStore,
  LocalCoworkYdocTransport,
} from "../persistence/LocalCoworkYdocTransport";
import {
  CoworkScratchRegistry,
  PREVIOUS_EDITOR_SCRATCH_ID,
} from "./registry";

describe("CoworkScratchRegistry discard", () => {
  beforeEach(() => localStorage.clear());

  it("deletes the persisted bytes so a discarded recovered document cannot return", async () => {
    const backing = new InMemoryCoworkYdocBackingStore();
    const transportFactory = (scratchId: string) =>
      new LocalCoworkYdocTransport({
        documentId: scratchId,
        factory: () => backing,
      });
    const previousEditor = transportFactory(PREVIOUS_EDITOR_SCRATCH_ID);
    const base = await previousEditor.pull({});
    await previousEditor.push({
      batch: new Uint8Array([7, 8, 9]),
      baseSha256: base.docSha256,
      baseYdocGeneration: base.ydocGeneration,
    });
    const registry = new CoworkScratchRegistry(localStorage, transportFactory);

    await expect(registry.discoverPreviousEditorScratch()).resolves.toMatchObject({
      scratchId: PREVIOUS_EDITOR_SCRATCH_ID,
      recoveredFromPreviousEditor: true,
    });
    await registry.discard(PREVIOUS_EDITOR_SCRATCH_ID);

    expect(registry.list()).toEqual([]);
    expect((await previousEditor.pull({})).batches).toEqual([]);
    await expect(
      new CoworkScratchRegistry(
        localStorage,
        transportFactory,
      ).discoverPreviousEditorScratch(),
    ).resolves.toBeNull();
  });

  it("gives multiple new on-device documents distinct human titles", () => {
    const registry = new CoworkScratchRegistry(localStorage, () => ({
      pull: async () => ({
        snapshot: null,
        snapshotSha256: null,
        batches: [],
        docSha256: "empty",
        nextOffset: "0",
        ydocGeneration: "local",
      }),
      delete: async () => undefined,
    }));

    expect(registry.create().title).toBe("Untitled");
    expect(registry.create().title).toBe("Untitled 2");
    expect(registry.create().title).toBe("Untitled 3");
  });

  it("updates recency metadata when an on-device document is edited", () => {
    const registry = new CoworkScratchRegistry(localStorage, () => ({
      pull: async () => ({
        snapshot: null,
        snapshotSha256: null,
        batches: [],
        docSha256: "empty",
        nextOffset: "0",
        ydocGeneration: "local",
      }),
      delete: async () => undefined,
    }));
    const first = registry.create();
    const second = registry.create();

    const touched = registry.touch(first.scratchId, "2030-01-02T03:04:05.000Z");

    expect(touched.updatedAt).toBe("2030-01-02T03:04:05.000Z");
    expect(registry.list().map((entry) => entry.scratchId)).toEqual([
      first.scratchId,
      second.scratchId,
    ]);
  });
});
