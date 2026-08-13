import { act, render, screen, waitFor } from "@testing-library/react";
import type { Editor } from "@tiptap/core";
import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";

import { bootstrapCoworkYdoc } from "../documents/bootstrapCoworkYdoc";
import { InMemoryCoworkYdocTransport } from "../persistence/InMemoryCoworkYdocTransport";
import { CoworkBridgeEditor } from "./CoworkBridgeEditor";

describe("CoworkBridgeEditor provenance persistence settlement", () => {
  it("reports an unchanged generation only after normal persistence compacts it", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("Initial text"),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    const server = new InMemoryCoworkYdocTransport();
    const empty = await server.pull({});
    await server.push({
      batch: initialized.snapshot,
      baseSha256: empty.docSha256,
      baseStructuredHeadSha256: empty.structuredHeadSha256,
      baseYdocGeneration: empty.ydocGeneration,
      compaction: {
        snapshot: initialized.snapshot,
        snapshotSha256: initialized.snapshotSha256,
      },
    });
    const document = new Y.Doc();
    let editor: Editor | null = null;
    const settled = vi.fn();
    const mounted = render(
      <CoworkBridgeEditor
        document={document}
        transport={server}
        seedMarkdown=""
        onReady={(context) => {
          editor = context.editor;
        }}
        onProvenancePersistenceSettled={settled}
      />,
    );
    await screen.findByRole(
      "textbox",
      { name: "Document editor" },
      { timeout: 10_000 },
    );
    await waitFor(() => expect(editor).not.toBeNull());
    settled.mockClear();

    act(() => {
      editor!.commands.insertContentAt(editor!.state.doc.content.size, " changed");
    });

    await waitFor(() => expect(settled).toHaveBeenCalled(), { timeout: 10_000 });
    const serverHead = (await server.pull({})).structuredHeadSha256;
    expect(settled).toHaveBeenLastCalledWith(serverHead);

    mounted.unmount();
    document.destroy();
  });
});
