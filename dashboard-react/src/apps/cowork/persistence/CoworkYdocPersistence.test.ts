import { describe, expect, it } from "vitest";
import * as Y from "yjs";

import { applyWithOrigin } from "../editor/applyOrigin";
import { CoworkYdocPersistence } from "./CoworkYdocPersistence";
import { InMemoryCoworkYdocTransport } from "./InMemoryCoworkYdocTransport";

const seedTransport = async (
  transport: InMemoryCoworkYdocTransport,
  build: (doc: Y.Doc) => void,
): Promise<void> => {
  const server = new Y.Doc();
  build(server);
  const update = Y.encodeStateAsUpdate(server);
  const base = await transport.pull({});
  await transport.push({ batch: update, baseSha256: base.docSha256 });
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

    clientDoc.getText("t").insert(5, " world");
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
    writerDoc.getText("t").insert(4, "!");
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
});
