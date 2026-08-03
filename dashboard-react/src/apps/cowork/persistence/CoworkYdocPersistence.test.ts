import { describe, expect, it, vi } from "vitest";
import { ySyncPluginKey } from "@tiptap/y-tiptap";
import * as Y from "yjs";

import { applyWithOrigin } from "../editor/applyOrigin";
import { CoworkYdocPersistence } from "./CoworkYdocPersistence";
import {
  DurableCoworkYdocOutbox,
  InMemoryCoworkYdocOutboxBackingStore,
} from "./CoworkYdocOutbox";
import { InMemoryCoworkYdocTransport } from "./InMemoryCoworkYdocTransport";
import { sha256Hex } from "./hashing";
import type {
  CoworkYdocPull,
  CoworkYdocPullRequest,
  CoworkYdocPushRequest,
  CoworkYdocPushResult,
  CoworkYdocTransport,
} from "./transport";

// A live human keystroke reaches the Y.Doc through the y-tiptap Collaboration binding
// under the ySyncPluginKey origin. With no editor mounted, a unit test reproduces that
// origin by transacting under it, so the persistence layer classifies the edit as human.
const humanEdit = (doc: Y.Doc, mutate: () => void): void =>
  doc.transact(mutate, ySyncPluginKey);

const seedTransport = async (
  transport: InMemoryCoworkYdocTransport,
  build: (doc: Y.Doc) => void,
): Promise<void> => {
  const server = new Y.Doc();
  build(server);
  const update = Y.encodeStateAsUpdate(server);
  const base = await transport.pull({});
  await transport.push({
    batch: update,
    baseSha256: base.docSha256,
    baseYdocGeneration: base.ydocGeneration,
  });
};

describe("CoworkYdocPersistence", () => {
  it("hydrates a Y.Doc from the transport before the editor is mounted", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    await seedTransport(transport, (doc) => doc.getText("t").insert(0, "hello"));

    const clientDoc = new Y.Doc();
    const persistence = new CoworkYdocPersistence(clientDoc, transport);
    await persistence.hydrate();

    expect(clientDoc.getText("t").toString()).toBe("hello");
  });

  it("pushes local human edits so another client can pull them", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    await seedTransport(transport, (doc) => doc.getText("t").insert(0, "hello"));

    const clientDoc = new Y.Doc();
    const persistence = new CoworkYdocPersistence(clientDoc, transport);
    await persistence.hydrate();
    persistence.start();

    humanEdit(clientDoc, () => clientDoc.getText("t").insert(5, " world"));
    await persistence.flush();

    const otherDoc = new Y.Doc();
    const other = new CoworkYdocPersistence(otherDoc, transport);
    await other.hydrate();
    expect(otherDoc.getText("t").toString()).toBe("hello world");
  });

  it("never pushes apply-origin mutations (ledger-derived, not human keystrokes)", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    await seedTransport(transport, (doc) => doc.getText("t").insert(0, "hi"));

    const clientDoc = new Y.Doc();
    const persistence = new CoworkYdocPersistence(clientDoc, transport);
    await persistence.hydrate();
    persistence.start();

    const shaBefore = transport.docSha256;
    applyWithOrigin(clientDoc, () => clientDoc.getText("t").insert(0, "AI "));
    await persistence.flush();

    // The apply-origin edit changed the local doc but was never pushed.
    expect(clientDoc.getText("t").toString()).toBe("AI hi");
    expect(transport.docSha256).toBe(shaBefore);
  });

  it("applies remote batches through an offset-sliced pull", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    await seedTransport(transport, (doc) => doc.getText("t").insert(0, "base"));

    const readerDoc = new Y.Doc();
    const reader = new CoworkYdocPersistence(readerDoc, transport);
    await reader.hydrate();

    const writerDoc = new Y.Doc();
    const writer = new CoworkYdocPersistence(writerDoc, transport);
    await writer.hydrate();
    writer.start();
    humanEdit(writerDoc, () => writerDoc.getText("t").insert(4, "!"));
    await writer.flush();

    await reader.pullSince();
    expect(readerDoc.getText("t").toString()).toBe("base!");
  });

  it("compacts the local state into a snapshot a fresh client can hydrate from", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    await seedTransport(transport, (doc) => doc.getText("t").insert(0, "content"));

    const clientDoc = new Y.Doc();
    const persistence = new CoworkYdocPersistence(clientDoc, transport);
    await persistence.hydrate();
    await persistence.compact();

    expect(transport.hasSnapshot).toBe(true);
    expect(transport.pendingBatchCount).toBe(0);

    const freshDoc = new Y.Doc();
    const fresh = new CoworkYdocPersistence(freshDoc, transport);
    await fresh.hydrate();
    expect(freshDoc.getText("t").toString()).toBe("content");
  });

  it("binds an exact Markdown projection to its capture compaction", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    await seedTransport(transport, (doc) => doc.getText("t").insert(0, "content"));
    const clientDoc = new Y.Doc();
    const persistence = new CoworkYdocPersistence(clientDoc, transport);
    await persistence.hydrate();
    const snapshot = Y.encodeStateAsUpdate(clientDoc);
    const snapshotSha256 = await sha256Hex(snapshot);
    const projectionMarkdown = "# Content\n\ncontent\n";
    const projectionSha256 = await sha256Hex(
      new TextEncoder().encode(projectionMarkdown),
    );

    const receipt = await persistence.compactProjection({
      snapshot,
      snapshotSha256,
      projectionMarkdown,
      projectionSha256,
    });

    expect(receipt).toMatchObject({
      snapshotSha256,
      compactedProjectionSha256: projectionSha256,
    });
    expect(receipt?.projectionReceiptId).toBeTruthy();
  });

  it("does not compact an old capture after a newer local edit is drained", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    await seedTransport(transport, (doc) => doc.getText("t").insert(0, "base"));
    const clientDoc = new Y.Doc();
    const persistence = new CoworkYdocPersistence(clientDoc, transport);
    await persistence.hydrate();
    persistence.start();
    const staleSnapshot = Y.encodeStateAsUpdate(clientDoc);
    const staleProjection = "base";

    humanEdit(clientDoc, () => clientDoc.getText("t").insert(4, " local"));
    const receipt = await persistence.compactProjection({
      snapshot: staleSnapshot,
      snapshotSha256: await sha256Hex(staleSnapshot),
      projectionMarkdown: staleProjection,
      projectionSha256: await sha256Hex(
        new TextEncoder().encode(staleProjection),
      ),
    });

    expect(receipt).toBeNull();
    expect(transport.hasSnapshot).toBe(false);
    const freshDoc = new Y.Doc();
    const fresh = new CoworkYdocPersistence(freshDoc, transport);
    await fresh.hydrate();
    expect(freshDoc.getText("t").toString()).toBe("base local");
  });

  it("does not reuse an old projection when a capture compaction is stale", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    await seedTransport(transport, (doc) => doc.getText("t").insert(0, "base"));
    const staleDoc = new Y.Doc();
    const stale = new CoworkYdocPersistence(staleDoc, transport);
    await stale.hydrate();
    const staleSnapshot = Y.encodeStateAsUpdate(staleDoc);
    const staleProjection = "base";

    const writerDoc = new Y.Doc();
    const writer = new CoworkYdocPersistence(writerDoc, transport);
    await writer.hydrate();
    writer.start();
    humanEdit(writerDoc, () => writerDoc.getText("t").insert(4, " remote"));
    await writer.flush();

    const first = await stale.compactProjection({
      snapshot: staleSnapshot,
      snapshotSha256: await sha256Hex(staleSnapshot),
      projectionMarkdown: staleProjection,
      projectionSha256: await sha256Hex(
        new TextEncoder().encode(staleProjection),
      ),
    });

    expect(first).toBeNull();
    expect(staleDoc.getText("t").toString()).toBe("base remote");
    const refreshedSnapshot = Y.encodeStateAsUpdate(staleDoc);
    const refreshedProjection = "base remote";
    const second = await stale.compactProjection({
      snapshot: refreshedSnapshot,
      snapshotSha256: await sha256Hex(refreshedSnapshot),
      projectionMarkdown: refreshedProjection,
      projectionSha256: await sha256Hex(
        new TextEncoder().encode(refreshedProjection),
      ),
    });
    expect(second?.compactedProjectionSha256).toBe(
      await sha256Hex(new TextEncoder().encode(refreshedProjection)),
    );
  });

  it("retries an offline tab after another tab compacts the same Y.Doc generation", async () => {
    const server = new InMemoryCoworkYdocTransport();
    await seedTransport(server, (doc) => doc.getText("t").insert(0, "base"));
    let online = false;
    const offlineTransport: CoworkYdocTransport = {
      pull: (request) => server.pull(request),
      push: (request) =>
        online ? server.push(request) : Promise.reject(new TypeError("offline")),
    };
    const outbox = new DurableCoworkYdocOutbox(
      "store:cross-tab-compaction",
      new InMemoryCoworkYdocOutboxBackingStore(),
    );

    const offlineDoc = new Y.Doc();
    const offlineTab = new CoworkYdocPersistence(offlineDoc, offlineTransport, {
      outbox,
    });
    await offlineTab.hydrate();
    offlineTab.start();
    humanEdit(offlineDoc, () => offlineDoc.getText("t").insert(4, " offline"));
    await expect(offlineTab.flush()).rejects.toThrow("offline");
    const [pending] = await outbox.list();
    expect(pending).toMatchObject({ acknowledged: false });

    const compactingDoc = new Y.Doc();
    const compactingTab = new CoworkYdocPersistence(compactingDoc, server);
    await compactingTab.hydrate();
    await compactingTab.compact();
    expect((await server.pull({})).ydocGeneration).toBe(pending.generation);

    online = true;
    await offlineTab.retry();
    expect(await outbox.list()).toMatchObject([{ acknowledged: true }]);

    const verifiedDoc = new Y.Doc();
    const verified = new CoworkYdocPersistence(verifiedDoc, server);
    await verified.hydrate();
    expect(verifiedDoc.getText("t").toString()).toBe("base offline");
  });

  it("keeps a rejected update queued and lets later work recover the chain", async () => {
    const backing = new InMemoryCoworkYdocTransport();
    await seedTransport(backing, (doc) => doc.getText("t").insert(0, "base"));
    let failNextPush = true;
    const flaky: CoworkYdocTransport = {
      pull: (request: CoworkYdocPullRequest): Promise<CoworkYdocPull> =>
        backing.pull(request),
      push: (request: CoworkYdocPushRequest): Promise<CoworkYdocPushResult> => {
        if (failNextPush) {
          failNextPush = false;
          return Promise.reject(new Error("offline"));
        }
        return backing.push(request);
      },
    };
    const clientDoc = new Y.Doc();
    const persistence = new CoworkYdocPersistence(clientDoc, flaky);
    await persistence.hydrate();
    persistence.start();

    humanEdit(clientDoc, () => clientDoc.getText("t").insert(4, " one"));
    await expect(persistence.flush()).rejects.toThrow("offline");
    expect(persistence.pendingBatchCount).toBe(1);

    humanEdit(clientDoc, () => clientDoc.getText("t").insert(8, " two"));
    await persistence.flush();
    expect(persistence.pendingBatchCount).toBe(0);
    expect(persistence.lastError).toBeNull();

    const reloaded = new Y.Doc();
    const reader = new CoworkYdocPersistence(reloaded, backing);
    await reader.hydrate();
    expect(reloaded.getText("t").toString()).toBe("base one two");
  });

  it("fails closed instead of merging a replacement snapshot into OLD plus a local edit", async () => {
    const oldServer = new Y.Doc();
    oldServer.getText("t").insert(0, "OLD");
    const oldSnapshot = Y.encodeStateAsUpdate(oldServer);
    const newServer = new Y.Doc();
    newServer.getText("t").insert(0, "NEW");
    const newSnapshot = Y.encodeStateAsUpdate(newServer);
    let pullCount = 0;
    const transport: CoworkYdocTransport = {
      pull: async (request) => {
        pullCount += 1;
        if (request.sinceOffset === undefined) {
          return {
            snapshot: oldSnapshot,
            snapshotSha256: "old-snapshot",
            ydocGeneration: "generation-old",
            batches: [],
            docSha256: "old-head",
            structuredHeadSha256: "old-head",
            nextOffset: "old-generation:0",
          };
        }
        return {
          snapshot: newSnapshot,
          snapshotSha256: "new-snapshot",
          ydocGeneration: "generation-new",
          batches: [],
          docSha256: "new-head",
          structuredHeadSha256: "new-head",
          cursorReset: true,
          nextOffset: "new-generation:0",
        };
      },
      push: async () => ({
        ok: false,
        error: "stale_base",
        serverDocSha256: "new-head",
        serverStructuredHeadSha256: "new-head",
      }),
    };
    const outbox = new DurableCoworkYdocOutbox(
      "store:replacement",
      new InMemoryCoworkYdocOutboxBackingStore(),
    );
    const document = new Y.Doc();
    const persistence = new CoworkYdocPersistence(document, transport, { outbox });
    await persistence.hydrate();
    persistence.start();

    humanEdit(document, () => document.getText("t").insert(3, " local"));
    await expect(persistence.flush()).rejects.toMatchObject({
      code: "cowork_ydoc_generation_changed",
    });

    expect(pullCount).toBe(2);
    expect(document.getText("t").toString()).toBe("OLD local");
    expect(persistence.offset).toBe("old-generation:0");
    expect(persistence.docSha256).toBe("old-head");
    expect(persistence.pendingBatchCount).toBe(1);
    expect(persistence.status).toBe("conflict");
    expect(await outbox.list()).toMatchObject([{ acknowledged: false }]);
  });

  it("does not replay an older-generation offline row into a fresh replacement session", async () => {
    const oldDocument = new Y.Doc();
    oldDocument.getText("t").insert(0, "OLD");
    let staleLocalUpdate = new Uint8Array();
    oldDocument.on("update", (update: Uint8Array) => {
      staleLocalUpdate = new Uint8Array(update);
    });
    oldDocument.getText("t").insert(3, " local");

    const outbox = new DurableCoworkYdocOutbox(
      "store:replacement-reload",
      new InMemoryCoworkYdocOutboxBackingStore(),
    );
    await outbox.append(staleLocalUpdate, "generation-old");

    const replacement = new Y.Doc();
    replacement.getText("t").insert(0, "NEW");
    const transport: CoworkYdocTransport = {
      pull: async () => ({
        snapshot: Y.encodeStateAsUpdate(replacement),
        snapshotSha256: "new-snapshot",
        ydocGeneration: "generation-new",
        batches: [],
        docSha256: "new-head",
        structuredHeadSha256: "new-head",
        nextOffset: "new-generation:0",
      }),
      push: async () => {
        throw new Error("push should not run during hydration");
      },
    };
    const document = new Y.Doc();
    const persistence = new CoworkYdocPersistence(document, transport, { outbox });

    await expect(persistence.hydrate()).rejects.toMatchObject({
      code: "cowork_ydoc_generation_changed",
    });
    expect(document.getText("t").toString()).toBe("NEW");
    expect(persistence.status).toBe("conflict");
    expect(await outbox.list()).toMatchObject([
      { acknowledged: false, generation: "generation-old" },
    ]);
  });

  it("rehydrates the durable outbox and prunes acknowledged history only after compaction", async () => {
    const transport = new InMemoryCoworkYdocTransport();
    await seedTransport(transport, (doc) => doc.getText("t").insert(0, "base"));
    const backing = new InMemoryCoworkYdocOutboxBackingStore();
    const firstOutbox = new DurableCoworkYdocOutbox("store:doc", backing);
    const firstDoc = new Y.Doc();
    const first = new CoworkYdocPersistence(firstDoc, transport, {
      outbox: firstOutbox,
    });
    await first.hydrate();
    first.start();
    humanEdit(firstDoc, () => firstDoc.getText("t").insert(4, " local"));
    await first.flush();

    expect(await firstOutbox.list()).toMatchObject([
      { acknowledged: true },
    ]);

    const reopenedOutbox = new DurableCoworkYdocOutbox("store:doc", backing);
    const reopenedDoc = new Y.Doc();
    const reopened = new CoworkYdocPersistence(reopenedDoc, transport, {
      outbox: reopenedOutbox,
    });
    await reopened.hydrate();
    expect(reopenedDoc.getText("t").toString()).toBe("base local");
    expect(reopened.pendingBatchCount).toBe(0);

    await reopened.compact();
    expect(await reopenedOutbox.list()).toEqual([]);
  });

  it("replays an offline durable outbox in order on explicit reconnect retry", async () => {
    const backing = new InMemoryCoworkYdocTransport();
    await seedTransport(backing, (doc) => doc.getText("t").insert(0, "base"));
    let online = false;
    const flaky: CoworkYdocTransport = {
      pull: (request) => backing.pull(request),
      push: (request) =>
        online ? backing.push(request) : Promise.reject(new TypeError("offline")),
    };
    const outboxBacking = new InMemoryCoworkYdocOutboxBackingStore();
    const outbox = new DurableCoworkYdocOutbox("store:offline-doc", outboxBacking);
    const document = new Y.Doc();
    const persistence = new CoworkYdocPersistence(document, flaky, { outbox });
    await persistence.hydrate();
    persistence.start();

    humanEdit(document, () => document.getText("t").insert(4, " one"));
    await expect(persistence.flush()).rejects.toThrow("offline");
    humanEdit(document, () => document.getText("t").insert(8, " two"));
    await expect(persistence.flush()).rejects.toThrow("offline");
    expect(await outbox.list()).toMatchObject([
      { acknowledged: false },
      { acknowledged: false },
    ]);

    online = true;
    await persistence.retry();
    await persistence.flush();

    expect(await outbox.list()).toMatchObject([
      { acknowledged: true },
      { acknowledged: true },
    ]);
    const reloaded = new Y.Doc();
    const reader = new CoworkYdocPersistence(reloaded, backing);
    await reader.hydrate();
    expect(reloaded.getText("t").toString()).toBe("base one two");
  });

  it("restores unacknowledged offline edits before mount after a reload", async () => {
    const server = new InMemoryCoworkYdocTransport();
    await seedTransport(server, (doc) => doc.getText("t").insert(0, "base"));
    let online = false;
    let successfulPushes = 0;
    const flaky: CoworkYdocTransport = {
      pull: (request) => server.pull(request),
      push: async (request) => {
        if (!online) throw new TypeError("offline");
        successfulPushes += 1;
        return server.push(request);
      },
    };
    const outboxBacking = new InMemoryCoworkYdocOutboxBackingStore();
    const firstOutbox = new DurableCoworkYdocOutbox(
      "store:reload-offline",
      outboxBacking,
    );
    const firstDoc = new Y.Doc();
    const first = new CoworkYdocPersistence(firstDoc, flaky, {
      outbox: firstOutbox,
    });
    await first.hydrate();
    first.start();

    humanEdit(firstDoc, () => firstDoc.getText("t").insert(4, " one"));
    await expect(first.flush()).rejects.toThrow("offline");
    humanEdit(firstDoc, () => firstDoc.getText("t").insert(8, " two"));
    await expect(first.flush()).rejects.toThrow("offline");
    await first.dispose();

    const reopenedOutbox = new DurableCoworkYdocOutbox(
      "store:reload-offline",
      outboxBacking,
    );
    const reopenedDoc = new Y.Doc();
    const reopened = new CoworkYdocPersistence(reopenedDoc, flaky, {
      outbox: reopenedOutbox,
    });
    const hydration = await reopened.hydrate();

    // This assertion happens before start() and therefore before any retry: the editor can
    // mount with the exact device-local state even while the server is still unreachable.
    expect(hydration.wasEmpty).toBe(false);
    expect(reopenedDoc.getText("t").toString()).toBe("base one two");
    expect(reopened.pendingBatchCount).toBe(2);
    expect(reopened.status).toBe("saved_on_device");

    await reopened.hydrate();
    expect(reopenedDoc.getText("t").toString()).toBe("base one two");
    expect(reopened.pendingBatchCount).toBe(2);

    online = true;
    await reopened.retry();
    expect(successfulPushes).toBe(1);
    expect(reopened.pendingBatchCount).toBe(0);
    expect((await reopenedOutbox.list()).every((entry) => entry.acknowledged)).toBe(
      true,
    );

    // A subsequent reload sees the now-server-backed update and the retained acknowledged
    // rows. Yjs application is idempotent, so recovery cannot duplicate visible content.
    const verifiedDoc = new Y.Doc();
    const verified = new CoworkYdocPersistence(verifiedDoc, flaky, {
      outbox: new DurableCoworkYdocOutbox(
        "store:reload-offline",
        outboxBacking,
      ),
    });
    await verified.hydrate();
    expect(verifiedDoc.getText("t").toString()).toBe("base one two");
    expect(verified.pendingBatchCount).toBe(0);
  });

  it("merges a typing burst into one push and acknowledges every covered row only after success", async () => {
    const backing = new InMemoryCoworkYdocTransport();
    await seedTransport(backing, (doc) => doc.getText("t").insert(0, ""));
    let releasePush!: () => void;
    const gate = new Promise<void>((resolve) => {
      releasePush = resolve;
    });
    let pushCount = 0;
    const delayed: CoworkYdocTransport = {
      pull: (request) => backing.pull(request),
      push: async (request) => {
        pushCount += 1;
        await gate;
        return backing.push(request);
      },
    };
    const outboxBacking = new InMemoryCoworkYdocOutboxBackingStore();
    const outbox = new DurableCoworkYdocOutbox("store:burst", outboxBacking);
    const document = new Y.Doc();
    const persistence = new CoworkYdocPersistence(document, delayed, { outbox });
    await persistence.hydrate();
    persistence.start();

    for (const character of "Exact live Save marker") {
      humanEdit(document, () => {
        const text = document.getText("t");
        text.insert(text.length, character);
      });
    }
    const flushing = persistence.flush();
    await vi.waitFor(() => expect(pushCount).toBe(1));
    const beforeAck = await outbox.list();
    expect(beforeAck).toHaveLength(22);
    expect(beforeAck.every((entry) => !entry.acknowledged)).toBe(true);

    releasePush();
    await flushing;
    expect(pushCount).toBe(1);
    expect((await outbox.list()).every((entry) => entry.acknowledged)).toBe(true);
  });

  it("retains and reports an update whose device-local append fails until retry makes it durable", async () => {
    const backing = new InMemoryCoworkYdocTransport();
    let allowAppend = false;
    let nextId = 1;
    let pushCount = 0;
    const transport: CoworkYdocTransport = {
      pull: (request) => backing.pull(request),
      push: (request) => {
        pushCount += 1;
        return backing.push(request);
      },
    };
    const outbox = {
      list: async () => [],
      append: async (batch: Uint8Array) => {
        if (!allowAppend) throw new Error("IndexedDB append failed");
        return {
          id: nextId++,
          batch: new Uint8Array(batch),
          acknowledged: false,
        };
      },
      acknowledge: async () => undefined,
      pruneAcknowledged: async () => undefined,
    };
    const document = new Y.Doc();
    const persistence = new CoworkYdocPersistence(document, transport, { outbox });
    await persistence.hydrate();
    persistence.start();

    humanEdit(document, () => document.getText("t").insert(0, "retained"));
    await expect(persistence.ensureDeviceDurability()).rejects.toThrow(
      "IndexedDB append failed",
    );
    expect(pushCount).toBe(0);
    expect(persistence.status).toBe("error");
    expect(persistence.lastError).toBeInstanceOf(Error);

    allowAppend = true;
    await persistence.ensureDeviceDurability();
    expect(pushCount).toBe(0);
    expect(persistence.pendingBatchCount).toBe(1);
    expect(persistence.status).toBe("saved_on_device");
    await persistence.retry();
    expect(pushCount).toBe(1);
    expect(persistence.pendingBatchCount).toBe(0);
    expect(persistence.status).toBe("clean");
    expect(persistence.lastError).toBeNull();

    const reloaded = new Y.Doc();
    const reader = new CoworkYdocPersistence(reloaded, backing);
    await reader.hydrate();
    expect(reloaded.getText("t").toString()).toBe("retained");
  });

  it("does not report clean when the durable acknowledgement fails after server success", async () => {
    const backing = new InMemoryCoworkYdocTransport();
    let allowAcknowledge = false;
    let pushCount = 0;
    const transport: CoworkYdocTransport = {
      pull: (request) => backing.pull(request),
      push: (request) => {
        pushCount += 1;
        return backing.push(request);
      },
    };
    const outbox = {
      list: async () => [],
      append: async (batch: Uint8Array) => ({
        id: 1,
        batch: new Uint8Array(batch),
        acknowledged: false,
      }),
      acknowledge: async () => {
        if (!allowAcknowledge) throw new Error("IndexedDB acknowledgement failed");
      },
      pruneAcknowledged: async () => undefined,
    };
    const document = new Y.Doc();
    const persistence = new CoworkYdocPersistence(document, transport, { outbox });
    await persistence.hydrate();
    persistence.start();

    humanEdit(document, () => document.getText("t").insert(0, "server accepted"));
    await expect(persistence.flush()).rejects.toThrow(
      "IndexedDB acknowledgement failed",
    );
    expect(pushCount).toBe(1);
    expect(persistence.pendingBatchCount).toBe(1);
    expect(persistence.status).toBe("error");
    expect(persistence.lastError).toBeInstanceOf(Error);

    allowAcknowledge = true;
    await persistence.retry();
    expect(pushCount).toBe(2);
    expect(persistence.pendingBatchCount).toBe(0);
    expect(persistence.status).toBe("clean");
    expect(persistence.lastError).toBeNull();
  });
});
