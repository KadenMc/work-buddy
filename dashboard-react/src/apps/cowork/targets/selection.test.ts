import { Editor } from "@tiptap/core";
import { TextSelection } from "@tiptap/pm/state";
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
  it("preserves a backwards selection's exact endpoints and resolves its containing section", async () => {
    const current = await open(
      "# Intro\n\nOpening.\n\n# Risks\n\nFirst risk.\n\nSecond risk.\n\n# Next\n\nDone.",
    );
    const risk = resolveQuoteAnchor(current.state.doc, {
      exact: "First risk.",
      prefix: "",
      suffix: "",
    });
    if (risk === null) throw new Error("fixture did not resolve");
    current.view.dispatch(
      current.state.tr.setSelection(
        TextSelection.create(current.state.doc, risk.to, risk.from),
      ),
    );

    const selection = coworkSelectionRange(current);
    const section = coworkCurrentSectionRange(current);

    expect(selection).toMatchObject({
      from: risk.from,
      to: risk.to,
      granularity: "character",
    });
    expect(
      current.state.doc.textBetween(selection!.from, selection!.to, " "),
    ).toBe("First risk.");
    expect(
      current.state.doc.textBetween(section!.from, section!.to, " "),
    ).toContain("Risks First risk. Second risk.");
    expect(
      current.state.doc.textBetween(section!.from, section!.to, " "),
    ).not.toContain("Next");
  });

  it("maps exact partial selections through bold, link, inline code, and emoji syntax", async () => {
    const current = await open(
      "Before **bold target** and [linked target](https://example.com) plus `code target` 🚀 after.",
    );
    if (document === null) throw new Error("document did not open");
    const beforeJson = current.getJSON();
    const beforeYjs = Y.encodeStateAsUpdate(document);
    const cases = [
      { exact: "efore", expected: "efore" },
      { exact: "bold target", expected: "bold target" },
      { exact: "linked target", expected: "linked target" },
      { exact: "code target", expected: "code target" },
      { exact: "🚀", expected: "🚀" },
    ] as const;

    for (const candidate of cases) {
      const located = resolveQuoteAnchor(current.state.doc, {
        exact: candidate.exact,
        prefix: "",
        suffix: "",
      });
      if (located === null) {
        throw new Error(`fixture did not resolve ${candidate.exact}`);
      }
      current.commands.setTextSelection(located);
      const range = coworkSelectionRange(current);
      if (range === null) throw new Error("selection did not resolve");
      const projection = coworkProjectionTarget(current, document, range);
      if (projection.target.selector.kind !== "text_quote") {
        throw new Error("expected a text quote selector");
      }
      expect(projection.target.markdownText).toBe(candidate.expected);
      expect(projection.target.selector.exact).toBe(candidate.expected);
      expect(
        Array.from(projection.markdown)
          .slice(
            projection.target.selector.start,
            projection.target.selector.end,
          )
          .join(""),
      ).toBe(candidate.expected);
    }
    expect(current.getJSON()).toEqual(beforeJson);
    expect(Y.encodeStateAsUpdate(document)).toEqual(beforeYjs);
  });

  it("keeps exact cross-block/list endpoints while retaining intervening Markdown", async () => {
    const current = await open(
      "Paragraph one and two.\n\n- First item\n- Second item ending\n\nAfter paragraph.",
    );
    if (document === null) throw new Error("document did not open");
    const start = resolveQuoteAnchor(current.state.doc, {
      exact: "one and two",
      prefix: "",
      suffix: "",
    });
    const end = resolveQuoteAnchor(current.state.doc, {
      exact: "Second item",
      prefix: "",
      suffix: "",
    });
    if (start === null || end === null) throw new Error("fixture did not resolve");
    current.commands.setTextSelection({
      from: start.from + "one ".length,
      to: end.from + "Second".length,
    });
    const range = coworkSelectionRange(current);
    if (range === null) throw new Error("selection did not resolve");
    const projection = coworkProjectionTarget(current, document, range);
    if (projection.target.selector.kind !== "text_quote") {
      throw new Error("expected a text quote selector");
    }

    expect(projection.target.markdownText).toMatch(/^and two\./u);
    expect(projection.target.markdownText).toContain("- First item");
    expect(projection.target.markdownText).toMatch(/- Second$/u);
    expect(projection.target.markdownText).not.toContain("Paragraph one");
    expect(projection.target.markdownText).not.toContain("item ending");
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
