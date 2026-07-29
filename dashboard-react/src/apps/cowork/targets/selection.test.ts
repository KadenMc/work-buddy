import { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import { bootstrapCoworkYdoc } from "../documents/bootstrapCoworkYdoc";
import { buildEditorExtensions } from "../editor/extensions";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import {
  coworkCurrentSectionRange,
  coworkProjectionTarget,
  coworkSelectionRange,
} from "./selection";

let editor: Editor | null = null;
let document: Y.Doc | null = null;

const open = async (markdown: string): Promise<Editor> => {
  const initialized = await bootstrapCoworkYdoc(
    new TextEncoder().encode(markdown),
  );
  if (!initialized.ok) throw new Error(initialized.message);
  document = new Y.Doc();
  Y.applyUpdate(document, initialized.snapshot);
  editor = new Editor({
    element: window.document.createElement("div"),
    extensions: buildEditorExtensions(document),
  });
  return editor;
};

afterEach(() => {
  editor?.destroy();
  document?.destroy();
  editor = null;
  document = null;
});

describe("document-target selection helpers", () => {
  it("expands a selection to a top-level block and resolves the containing heading section", async () => {
    const current = await open(
      "# Intro\n\nOpening.\n\n# Risks\n\nFirst risk.\n\nSecond risk.\n\n# Next\n\nDone.",
    );
    const risk = resolveQuoteAnchor(current.state.doc, {
      exact: "First risk.",
      prefix: "",
      suffix: "",
    });
    if (risk === null) throw new Error("fixture did not resolve");
    current.commands.setTextSelection(risk);

    const selection = coworkSelectionRange(current);
    const section = coworkCurrentSectionRange(current);

    expect(selection).not.toBeNull();
    expect(
      current.state.doc.textBetween(selection!.from, selection!.to, " "),
    ).toContain("First risk.");
    expect(
      current.state.doc.textBetween(section!.from, section!.to, " "),
    ).toContain("Risks First risk. Second risk.");
    expect(
      current.state.doc.textBetween(section!.from, section!.to, " "),
    ).not.toContain("Next");
  });

  it("maps block ranges to Unicode code-point offsets in canonical Markdown", async () => {
    const current = await open(
      "---\ntitle: Demo\n---\n# Intro\n\nOpening.\n\n# Risks 🚀\n\nA **bold** risk.\n\n# Next\n\nDone.\n",
    );
    const risk = resolveQuoteAnchor(current.state.doc, {
      exact: "bold",
      prefix: "",
      suffix: "",
    });
    if (risk === null || document === null) throw new Error("fixture did not resolve");
    current.commands.setTextSelection(risk);
    const section = coworkCurrentSectionRange(current);
    if (section === null) throw new Error("section did not resolve");

    const projection = coworkProjectionTarget(current, document, section);
    if (projection.target.selector.kind !== "text_quote") {
      throw new Error("expected a text quote selector");
    }
    const selector = projection.target.selector;
    const byCodePoint = Array.from(projection.markdown)
      .slice(selector.start, selector.end)
      .join("");

    expect(byCodePoint).toBe(selector.exact);
    expect(selector.exact).toContain("# Risks 🚀");
    expect(selector.exact).toContain("A **bold** risk.");
    expect(selector.exact).not.toContain("# Next");
    // The emoji consumes two UTF-16 code units but one Truth selector offset.
    expect(selector.end - selector.start).toBe(Array.from(selector.exact).length);
  });
});
