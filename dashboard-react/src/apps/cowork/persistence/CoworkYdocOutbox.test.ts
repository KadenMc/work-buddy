import { describe, expect, it } from "vitest";

import {
  DurableCoworkYdocOutbox,
  InMemoryCoworkYdocOutboxBackingStore,
} from "./CoworkYdocOutbox";

describe("DurableCoworkYdocOutbox", () => {
  it("survives reconstruction and retains acknowledged updates until compaction", async () => {
    const backing = new InMemoryCoworkYdocOutboxBackingStore();
    const first = new DurableCoworkYdocOutbox("store:doc", backing);
    const one = await first.append(new Uint8Array([1, 2]));
    await first.append(new Uint8Array([3]));
    await first.acknowledge(one.id);

    const reopened = new DurableCoworkYdocOutbox("store:doc", backing);
    expect(await reopened.list()).toEqual([
      { id: 1, batch: new Uint8Array([1, 2]), acknowledged: true },
      { id: 2, batch: new Uint8Array([3]), acknowledged: false },
    ]);

    await reopened.pruneAcknowledged();
    expect(await reopened.list()).toEqual([
      { id: 2, batch: new Uint8Array([3]), acknowledged: false },
    ]);
  });

  it("keeps document keys isolated", async () => {
    const backing = new InMemoryCoworkYdocOutboxBackingStore();
    const one = new DurableCoworkYdocOutbox("one", backing);
    const two = new DurableCoworkYdocOutbox("two", backing);
    await one.append(new Uint8Array([1]));
    await two.append(new Uint8Array([2]));
    expect((await one.list())[0]?.batch).toEqual(new Uint8Array([1]));
    expect((await two.list())[0]?.batch).toEqual(new Uint8Array([2]));
  });

  it("allocates unique ordered ids for concurrent instances on the same key", async () => {
    const backing = new InMemoryCoworkYdocOutboxBackingStore();
    const left = new DurableCoworkYdocOutbox("shared", backing);
    const right = new DurableCoworkYdocOutbox("shared", backing);

    const appended = await Promise.all([
      left.append(new Uint8Array([10])),
      right.append(new Uint8Array([20])),
      left.append(new Uint8Array([30])),
      right.append(new Uint8Array([40])),
    ]);

    expect(appended.map((entry) => entry.id).sort((a, b) => a - b)).toEqual([
      1, 2, 3, 4,
    ]);
    expect((await left.list()).map((entry) => entry.batch[0]).sort()).toEqual([
      10, 20, 30, 40,
    ]);
  });

  it("merges concurrent acknowledgements instead of overwriting another instance", async () => {
    const backing = new InMemoryCoworkYdocOutboxBackingStore();
    const left = new DurableCoworkYdocOutbox("shared", backing);
    const right = new DurableCoworkYdocOutbox("shared", backing);
    const first = await left.append(new Uint8Array([1]));
    const second = await right.append(new Uint8Array([2]));

    await Promise.all([
      left.acknowledge(first.id),
      right.acknowledge(second.id),
    ]);

    expect(await left.list()).toMatchObject([
      { id: first.id, acknowledged: true },
      { id: second.id, acknowledged: true },
    ]);
  });

  it("cannot let a concurrent acknowledgement erase a newer append", async () => {
    const backing = new InMemoryCoworkYdocOutboxBackingStore();
    const left = new DurableCoworkYdocOutbox("shared", backing);
    const right = new DurableCoworkYdocOutbox("shared", backing);
    const first = await left.append(new Uint8Array([1]));

    await Promise.all([
      left.acknowledge(first.id),
      right.append(new Uint8Array([2])),
    ]);

    expect(await right.list()).toEqual([
      { id: 1, batch: new Uint8Array([1]), acknowledged: true },
      { id: 2, batch: new Uint8Array([2]), acknowledged: false },
    ]);
  });
});
