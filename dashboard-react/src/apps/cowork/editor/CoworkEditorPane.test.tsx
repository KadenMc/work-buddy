import { act, render, screen, waitFor, within } from "@testing-library/react";
import { ySyncPluginKey } from "@tiptap/y-tiptap";
import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";

import {
  InMemoryCoworkYdocBackingStore,
  LocalCoworkYdocTransport,
} from "../persistence/LocalCoworkYdocTransport";
import { InMemoryCoworkYdocTransport } from "../persistence/InMemoryCoworkYdocTransport";
import { sha256Hex } from "../persistence/hashing";
import type { CoworkYdocTransport } from "../persistence/transport";
import CoworkEditorPane, {
  type CoworkScratchPromotionHandle,
} from "./CoworkEditorPane";

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

  it("exports the exact visible scratch text and a matching valid Y.Doc snapshot", async () => {
    const documentId = "pane-promotion-doc";
    const marker = "Exact scratch content — keep every character.";
    const scratchDoc = new Y.Doc();
    const promotionHandle: { current: CoworkScratchPromotionHandle | null } = {
      current: null,
    };

    render(
      <CoworkEditorPane
        documentId={documentId}
        document={scratchDoc}
        onPromotionHandle={(handle) => {
          promotionHandle.current = handle;
        }}
      />,
    );
    const textbox = await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );
    act(() => {
      scratchDoc.transact(() => {
        const fragment = scratchDoc.getXmlFragment("default");
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
    await waitFor(() => expect(textbox.textContent).toContain(marker));
    await waitFor(() => expect(promotionHandle.current).not.toBeNull());

    const handle = promotionHandle.current;
    if (handle === null) throw new Error("The promotion handle did not become ready.");
    const promoted = await handle.exportContent();
    expect(new TextDecoder().decode(promoted.sourceBytes)).toBe(marker);

    const promotedDoc = new Y.Doc();
    expect(() => Y.applyUpdate(promotedDoc, promoted.snapshot)).not.toThrow();
    expect(promotedDoc.getXmlFragment("default").toString()).toContain(marker);
    expect(
      promotedDoc.getMap<unknown>("wb-cowork:fidelity").get("source_sha256"),
    ).toBe(await sha256Hex(promoted.sourceBytes));
    promotedDoc.destroy();
  }, 20_000);

  it("does not publish a promotion handle until hydration and editor mount complete", async () => {
    let releaseHydration!: () => void;
    const hydrationGate = new Promise<void>((resolve) => {
      releaseHydration = resolve;
    });
    const transport: CoworkYdocTransport = {
      pull: async () => {
        await hydrationGate;
        return {
          snapshot: null,
          snapshotSha256: null,
          ydocGeneration: "generation-delayed",
          batches: [],
          docSha256: "",
          structuredHeadSha256: "",
          projectionSha256: "",
          cursorReset: false,
          nextOffset: "0",
        };
      },
      push: async () => ({
        ok: true,
        applied: true,
        docSha256: "head-1",
        structuredHeadSha256: "head-1",
        ydocGeneration: "generation-delayed",
        projectionSha256: "",
        nextOffset: "1",
      }),
    };
    const handles: Array<CoworkScratchPromotionHandle | null> = [];
    render(
      <CoworkEditorPane
        documentId="delayed-promotion"
        transport={transport}
        onPromotionHandle={(handle) => handles.push(handle)}
      />,
    );

    await waitFor(() => expect(handles).toContain(null));
    expect(handles.some((handle) => handle !== null)).toBe(false);
    expect(screen.getByRole("status")).toHaveTextContent("Loading the document");

    releaseHydration();
    await screen.findByRole("textbox", { name: "Document editor" });
    await waitFor(() => expect(handles.some((handle) => handle !== null)).toBe(true));
  });

  it("reports a failed scratch device save and retries the full local snapshot", async () => {
    let allowPush = false;
    const backing = new InMemoryCoworkYdocTransport();
    const push = vi.fn(async (request: Parameters<CoworkYdocTransport["push"]>[0]) => {
      if (!allowPush) throw new Error("IndexedDB transaction failed");
      return backing.push(request);
    });
    const transport: CoworkYdocTransport = {
      pull: (request) => backing.pull(request),
      push,
    };
    const statuses: string[] = [];
    const handleRef: { current: CoworkScratchPromotionHandle | null } = { current: null };
    render(
      <CoworkEditorPane
        documentId="scratch-device-retry"
        transport={transport}
        onSyncStatus={(status) => statuses.push(status)}
        onPromotionHandle={(handle) => {
          handleRef.current = handle;
        }}
      />,
    );
    await screen.findByRole("textbox", { name: "Document editor" });
    await waitFor(() => expect(statuses).toContain("error"));
    await waitFor(() => expect(handleRef.current).not.toBeNull());

    allowPush = true;
    await handleRef.current?.retryDeviceSave();
    expect(statuses[statuses.length - 1]).toBe("clean");
    expect(push).toHaveBeenCalledTimes(2);
    expect(backing.hasSnapshot).toBe(true);
  });
});
