import { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import { bootstrapCoworkYdoc } from "../documents/bootstrapCoworkYdoc";
import { buildEditorExtensions } from "../editor/extensions";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import {
  createCoworkCursorBoundaryReference,
  createCoworkDocumentTargetReference,
  resolveCoworkCursorBoundaryReference,
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
  it("round-trips a temporary cursor boundary at every character position", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode(
        "Alpha framing is here.\n\nSecond paragraph contains a custom passage.",
      ),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    document = new Y.Doc();
    Y.applyUpdate(document, initialized.snapshot);
    editor = new Editor({
      element: window.document.createElement("div"),
      extensions: buildEditorExtensions(document),
    });
    const located = resolveQuoteAnchor(editor.state.doc, {
      exact: "Second paragraph contains a custom passage.",
      prefix: "",
      suffix: "",
    });
    if (located === null) throw new Error("fixture did not resolve");

    const positions = [
      1,
      ...Array.from(
        { length: located.to - located.from + 1 },
        (_, index) => located.from + index,
      ),
    ];
    for (const position of positions) {
      const reference = createCoworkCursorBoundaryReference(
        editor,
        document,
        position,
      );
      expect(
        resolveCoworkCursorBoundaryReference(editor, document, reference),
        `position ${position.toString()}`,
      ).toBe(position);
    }
  });

  it("keeps a boundary at the first text position attached to its right-hand content", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("Alpha framing is here."),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    document = new Y.Doc();
    Y.applyUpdate(document, initialized.snapshot);
    editor = new Editor({
      element: window.document.createElement("div"),
      extensions: buildEditorExtensions(document),
    });

    const reference = createCoworkCursorBoundaryReference(
      editor,
      document,
      1,
    );
    expect(
      resolveCoworkCursorBoundaryReference(editor, document, reference),
    ).toBe(1);

    editor.commands.insertContentAt(1, "Prefixed ");

    expect(
      resolveCoworkCursorBoundaryReference(editor, document, reference),
    ).toBe(1 + "Prefixed ".length);
  });

  it("keeps a persistent exact range from position 1 attached through boundary and interior edits", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("Alpha target stays.\n\nOutside paragraph."),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    document = new Y.Doc();
    Y.applyUpdate(document, initialized.snapshot);
    editor = new Editor({
      element: window.document.createElement("div"),
      extensions: buildEditorExtensions(document),
    });
    const originalText = "Alpha target";
    editor.commands.setTextSelection({
      from: 1,
      to: 1 + originalText.length,
    });
    const range = coworkSelectionRange(editor);
    if (range === null) throw new Error("selection did not resolve");
    const reference = createCoworkDocumentTargetReference({
      editor,
      document,
      storeId: "store-a",
      documentId: "doc-a",
      range,
    });
    expect(reference.startBlockId).toBeTruthy();
    expect(reference.endBlockId).toBe(reference.startBlockId);

    const initial = resolveCoworkDocumentTargetReference(
      editor,
      document,
      reference,
    );
    expect(initial?.resolution).toBe("relative");
    expect(initial?.range.from).toBe(1);
    expect(
      editor.state.doc.textBetween(
        initial!.range.from,
        initial!.range.to,
        " ",
      ),
    ).toBe(originalText);

    editor.commands.insertContentAt(1, "Prefixed ");
    const afterStartInsert = resolveCoworkDocumentTargetReference(
      editor,
      document,
      reference,
    );
    expect(afterStartInsert?.resolution).toBe("relative");
    expect(
      editor.state.doc.textBetween(
        afterStartInsert!.range.from,
        afterStartInsert!.range.to,
        " ",
      ),
    ).toBe(originalText);

    editor.commands.insertContentAt(
      afterStartInsert!.range.from + "Alpha".length,
      " revised",
    );
    const afterInteriorEdit = resolveCoworkDocumentTargetReference(
      editor,
      document,
      reference,
    );
    expect(afterInteriorEdit?.resolution).toBe("relative");
    expect(
      editor.state.doc.textBetween(
        afterInteriorEdit!.range.from,
        afterInteriorEdit!.range.to,
        " ",
      ),
    ).toBe("Alpha revised target");

    editor.commands.insertContentAt(
      afterInteriorEdit!.range.to,
      " outside",
    );
    const afterEndInsert = resolveCoworkDocumentTargetReference(
      editor,
      document,
      reference,
    );
    expect(afterEndInsert?.resolution).toBe("relative");
    expect(
      editor.state.doc.textBetween(
        afterEndInsert!.range.from,
        afterEndInsert!.range.to,
        " ",
      ),
    ).toBe("Alpha revised target");
  });

  it("requires structural proof for the persistent position-1 fallback", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("Alpha target stays."),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    document = new Y.Doc();
    Y.applyUpdate(document, initialized.snapshot);
    editor = new Editor({
      element: window.document.createElement("div"),
      extensions: buildEditorExtensions(document),
    });
    editor.commands.setTextSelection({
      from: 1,
      to: 1 + "Alpha target".length,
    });
    const range = coworkSelectionRange(editor);
    if (range === null) throw new Error("selection did not resolve");
    const reference = createCoworkDocumentTargetReference({
      editor,
      document,
      storeId: "store-a",
      documentId: "doc-a",
      range,
    });

    // Make quote repair unavailable while the Yjs endpoints still identify
    // the edited range. Without both stable block IDs, the position-1
    // translation must remain rejected instead of weakening stale-ref safety.
    editor.commands.insertContentAt(1 + "Alpha".length, " revised");
    expect(
      resolveCoworkDocumentTargetReference(editor, document, {
        ...reference,
        startBlockId: undefined,
        endBlockId: undefined,
      }),
    ).toBeNull();
    expect(
      resolveCoworkDocumentTargetReference(editor, document, reference),
    ).toMatchObject({
      resolution: "relative",
      range: {
        from: 1,
        to: 1 + "Alpha revised target".length,
        granularity: "character",
      },
    });
  });

  it("fails closed when a persistent exact range from position 1 is deleted", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("Alpha target stays."),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    document = new Y.Doc();
    Y.applyUpdate(document, initialized.snapshot);
    editor = new Editor({
      element: window.document.createElement("div"),
      extensions: buildEditorExtensions(document),
    });
    const exact = {
      from: 1,
      to: 1 + "Alpha target".length,
    };
    editor.commands.setTextSelection(exact);
    const range = coworkSelectionRange(editor);
    if (range === null) throw new Error("selection did not resolve");
    const reference = createCoworkDocumentTargetReference({
      editor,
      document,
      storeId: "store-a",
      documentId: "doc-a",
      range,
    });

    editor.commands.deleteRange(exact);

    expect(
      resolveCoworkDocumentTargetReference(editor, document, reference),
    ).toBeNull();
  });

  it("keeps exact character endpoints attached when content is inserted before them", async () => {
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
    const exact = {
      from: located.from + "Target ".length,
      to: located.to - 1,
    };
    editor.commands.setTextSelection(exact);
    const range = coworkSelectionRange(editor);
    if (range === null) throw new Error("selection did not resolve");

    const reference = createCoworkDocumentTargetReference({
      editor,
      document,
      storeId: "store-a",
      documentId: "doc-a",
      range,
    });
    expect(reference.granularity).toBe("character");
    editor.commands.insertContentAt(exact.from, "Outside ");
    const afterStartInsert = resolveCoworkDocumentTargetReference(
      editor,
      document,
      reference,
    );
    expect(afterStartInsert?.resolution).toBe("relative");
    expect(
      editor.state.doc.textBetween(
        afterStartInsert!.range.from,
        afterStartInsert!.range.to,
        " ",
      ),
    ).toBe("paragraph");
    editor.commands.insertContentAt(afterStartInsert!.range.to, " outside");
    const afterEndInsert = resolveCoworkDocumentTargetReference(
      editor,
      document,
      reference,
    );
    expect(afterEndInsert?.resolution).toBe("relative");
    expect(
      editor.state.doc.textBetween(
        afterEndInsert!.range.from,
        afterEndInsert!.range.to,
        " ",
      ),
    ).toBe("paragraph");

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
    ).toBe("paragraph");
    expect(
      editor.state.doc.textBetween(
        resolved!.range.from,
        resolved!.range.to,
        " ",
      ),
    ).not.toContain("Inserted before.");
    expect(resolved?.range.granularity).toBe("character");
  });

  it("repairs an exact reference by quote without widening it", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("Before exact partial target after."),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    document = new Y.Doc();
    Y.applyUpdate(document, initialized.snapshot);
    editor = new Editor({
      element: window.document.createElement("div"),
      extensions: buildEditorExtensions(document),
    });
    const located = resolveQuoteAnchor(editor.state.doc, {
      exact: "partial target",
      prefix: "",
      suffix: "",
    });
    if (located === null) throw new Error("fixture did not resolve");
    editor.commands.setTextSelection(located);
    const range = coworkSelectionRange(editor);
    if (range === null) throw new Error("selection did not resolve");
    const reference = createCoworkDocumentTargetReference({
      editor,
      document,
      storeId: "store-a",
      documentId: "doc-a",
      range,
    });

    const repaired = resolveCoworkDocumentTargetReference(
      editor,
      document,
      {
        ...reference,
        relative: {
          startBase64: "not-base64",
          endBase64: "not-base64",
        },
      },
    );

    expect(repaired?.resolution).toBe("quote");
    expect(repaired?.range).toMatchObject({
      from: located.from,
      to: located.to,
      granularity: "character",
    });
    expect(
      editor.state.doc.textBetween(
        repaired!.range.from,
        repaired!.range.to,
        " ",
      ),
    ).toBe("partial target");
  });

  it("becomes unresolved when the exact passage is deleted instead of widening to its block", async () => {
    const initialized = await bootstrapCoworkYdoc(
      new TextEncoder().encode("Before exact partial target after."),
    );
    if (!initialized.ok) throw new Error(initialized.message);
    document = new Y.Doc();
    Y.applyUpdate(document, initialized.snapshot);
    editor = new Editor({
      element: window.document.createElement("div"),
      extensions: buildEditorExtensions(document),
    });
    const located = resolveQuoteAnchor(editor.state.doc, {
      exact: "partial target",
      prefix: "",
      suffix: "",
    });
    if (located === null) throw new Error("fixture did not resolve");
    editor.commands.setTextSelection(located);
    const range = coworkSelectionRange(editor);
    if (range === null) throw new Error("selection did not resolve");
    const reference = createCoworkDocumentTargetReference({
      editor,
      document,
      storeId: "store-a",
      documentId: "doc-a",
      range,
    });

    editor.commands.deleteRange(located);

    expect(
      resolveCoworkDocumentTargetReference(editor, document, reference),
    ).toBeNull();
  });
});
