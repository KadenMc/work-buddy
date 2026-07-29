import { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import { bootstrapCoworkYdoc } from "../documents/bootstrapCoworkYdoc";
import { buildEditorExtensions } from "../editor/extensions";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import {
  createCoworkDocumentTargetReference,
  resolveCoworkDocumentTargetReference,
} from "./relativeEndpoints";
import { coworkSelectionRange } from "./selection";

let editor: Editor | null = null;
let document: Y.Doc | null = null;

afterEach(() => {
  editor?.destroy();
  document?.destroy();
  editor = null;
  document = null;
});

describe("Yjs-relative document target endpoints", () => {
  it("keeps a block-aligned target attached when content is inserted before it", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("# Plan\n\nAlpha.\n\nTarget paragraph.\n\nOmega."),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    document = new Y.Doc();
    Y.applyUpdate(document, initialized.snapshot);
    editor = new Editor({
      element: window.document.createElement("div"),
      extensions: buildEditorExtensions(document),
    });
    const located = resolveQuoteAnchor(editor.state.doc, {
      exact: "Target paragraph.",
      prefix: "",
      suffix: "",
    });
    if (located === null) throw new Error("fixture did not resolve");
    editor.commands.setTextSelection(located);
    const range = coworkSelectionRange(editor);
    if (range === null) throw new Error("selection did not align");

    const reference = createCoworkDocumentTargetReference({
      editor,
      document,
      storeId: "store-a",
      documentId: "doc-a",
      range,
    });
    editor.commands.insertContentAt(0, {
      type: "paragraph",
      content: [{ type: "text", text: "Inserted before." }],
    });

    const resolved = resolveCoworkDocumentTargetReference(
      editor,
      document,
      reference,
    );
    expect(resolved?.resolution).toBe("relative");
    expect(
      editor.state.doc.textBetween(
        resolved!.range.from,
        resolved!.range.to,
        " ",
      ),
    ).toContain("Target paragraph.");
    expect(
      editor.state.doc.textBetween(
        resolved!.range.from,
        resolved!.range.to,
        " ",
      ),
    ).not.toContain("Inserted before.");
  });
});
