import { act, render, screen, waitFor, within } from "@testing-library/react";
import { ySyncPluginKey } from "@tiptap/y-tiptap";
import { describe, expect, it } from "vitest";
import * as Y from "yjs";

import {
  InMemoryCoworkYdocBackingStore,
  LocalCoworkYdocTransport,
} from "../persistence/LocalCoworkYdocTransport";
import CoworkEditorPane from "./CoworkEditorPane";

/** Reconstruct a Y.Doc from whatever the backing currently holds, as hydrate would. */
const reconstructFromBacking = async (
  factory: () => InMemoryCoworkYdocBackingStore,
  documentId: string,
): Promise<Y.Doc> => {
  const probe = new LocalCoworkYdocTransport({ documentId, factory });
  const pull = await probe.pull({});
  const doc = new Y.Doc();
  if (pull.snapshot !== null) Y.applyUpdate(doc, pull.snapshot);
  for (const batch of pull.batches) Y.applyUpdate(doc, batch);
  return doc;
};

describe("CoworkEditorPane persistence", () => {
  it("rehydrates a human-origin edit after unmount and remount on the same key", async () => {
    // One shared backing stands in for the persisted store that outlives a reload.
    const backing = new InMemoryCoworkYdocBackingStore();
    const factory = () => backing;
    const documentId = "pane-persistence-doc";
    const field = "wb-pane-probe";
    const marker = "persisted marker text";

    // First mount: a real editor over a controlled Y.Doc and a shared-backing transport.
    const firstDoc = new Y.Doc();
    const first = render(
      <CoworkEditorPane
        documentId={documentId}
        document={firstDoc}
        transport={new LocalCoworkYdocTransport({ documentId, factory })}
      />,
    );
    await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );

    // A transaction tagged with the y-sync origin is exactly what a live keystroke emits,
    // so persistence reads it as a human edit and pushes it through the transport.
    act(() => {
      firstDoc.transact(() => {
        firstDoc.getText(field).insert(0, marker);
      }, ySyncPluginKey);
    });

    // Wait until the edit is durable in the backing, so the remount cannot race the push.
    await waitFor(
      async () => {
        const persisted = await reconstructFromBacking(factory, documentId);
        expect(persisted.getText(field).toString()).toBe(marker);
      },
      { timeout: 10_000 },
    );

    first.unmount();

    // Remount with a brand-new Y.Doc and a fresh transport on the same key. Hydration must
    // reconstruct the edit from the persisted backing rather than start empty.
    const secondDoc = new Y.Doc();
    const second = render(
      <CoworkEditorPane
        documentId={documentId}
        document={secondDoc}
        transport={new LocalCoworkYdocTransport({ documentId, factory })}
      />,
    );
    await within(second.container).findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );

    await waitFor(
      () => expect(secondDoc.getText(field).toString()).toBe(marker),
      { timeout: 10_000 },
    );
  }, 20_000);

  it("survives a reload that happens before any idle compaction", async () => {
    // The collaborative binding writes the editor's base structure into the doc while
    // the editor is created, before persistence can observe it. This test edits INSIDE
    // that binding-created structure and remounts well before the idle compaction, so
    // it fails if the base is never persisted (orphaned updates over a missing base)
    // and passes only when the mount-time compaction anchors the full state.
    const backing = new InMemoryCoworkYdocBackingStore();
    const factory = () => backing;
    const documentId = "pane-fast-reload-doc";
    const marker = "fast reload marker";

    const firstDoc = new Y.Doc();
    const first = render(
      <CoworkEditorPane
        documentId={documentId}
        document={firstDoc}
        transport={new LocalCoworkYdocTransport({ documentId, factory })}
      />,
    );
    await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );

    // Insert text into the binding-created paragraph, the shape a live keystroke has:
    // an update whose ops reference structure the binding made during editor creation.
    act(() => {
      firstDoc.transact(() => {
        const fragment = firstDoc.getXmlFragment("default");
        const paragraph = fragment.get(0);
        if (paragraph instanceof Y.XmlElement) {
          paragraph.insert(0, [new Y.XmlText(marker)]);
        } else {
          const created = new Y.XmlElement("paragraph");
          created.insert(0, [new Y.XmlText(marker)]);
          fragment.insert(0, [created]);
        }
      }, ySyncPluginKey);
    });

    // Wait only for the push to land in the backing, never for the idle compaction.
    await waitFor(
      async () => {
        const persisted = await reconstructFromBacking(factory, documentId);
        expect(persisted.getXmlFragment("default").toString()).toContain(marker);
      },
      { timeout: 10_000 },
    );

    first.unmount();

    const secondDoc = new Y.Doc();
    const second = render(
      <CoworkEditorPane
        documentId={documentId}
        document={secondDoc}
        transport={new LocalCoworkYdocTransport({ documentId, factory })}
      />,
    );
    const textbox = await within(second.container).findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );
    await waitFor(
      () => expect(textbox.textContent ?? "").toContain(marker),
      { timeout: 10_000 },
    );
  }, 25_000);

  it("mounts a live editor on the default local transport when none is injected", async () => {
    // No injected transport, so the pane builds the default local one and still mounts a
    // real editor (the process-memory fallback stands in for IndexedDB under jsdom).
    render(<CoworkEditorPane documentId="pane-default-doc" />);
    await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );
  }, 15_000);
});
