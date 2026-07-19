import { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";

import { buildSchemaExtensions } from "./extensions";

const makeEditor = (): Editor =>
  new Editor({
    element: document.createElement("div"),
    extensions: buildSchemaExtensions(),
  });

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

describe("wb custom marks", () => {
  it("registers both wb marks in the schema", () => {
    editor = makeEditor();
    expect(editor.schema.marks.wbProvenanceTint).toBeDefined();
    expect(editor.schema.marks.wbExpressionMark).toBeDefined();
  });

  it("cannot reconstruct a provenance tint from pasted or imported HTML (forgery defense)", () => {
    editor = makeEditor();
    editor.commands.setContent(
      '<p><span class="wb-cowork-provenance-tint" data-wb-trust="ai-confirmed" data-producer="attacker">forged</span></p>',
    );
    let forged = false;
    editor.state.doc.descendants((node) => {
      if (node.marks.some((mark) => mark.type.name === "wbProvenanceTint")) {
        forged = true;
      }
    });
    expect(forged).toBe(false);
    // The text payload survives, only the mark is stripped.
    expect(editor.getText()).toContain("forged");
  });

  it("cannot reconstruct an expression mark from pasted or imported HTML", () => {
    editor = makeEditor();
    editor.commands.setContent(
      '<p><span class="wb-cowork-expression-mark" data-expression-id="e1" data-claim-ref="c1">claim</span></p>',
    );
    let forged = false;
    editor.state.doc.descendants((node) => {
      if (node.marks.some((mark) => mark.type.name === "wbExpressionMark")) {
        forged = true;
      }
    });
    expect(forged).toBe(false);
  });

  it("renders a provenance tint applied through the schema with its attrs and stamp class", () => {
    editor = makeEditor();
    editor
      .chain()
      .setContent("<p>confirmed span</p>")
      .selectAll()
      .setMark("wbProvenanceTint", {
        producer: "run-1",
        approval_gesture_id: "g-1",
      })
      .run();
    const html = editor.getHTML();
    expect(html).toContain("wb-cowork-provenance-tint");
    expect(html).toContain('data-producer="run-1"');
    expect(html).toContain('data-approval-gesture-id="g-1"');
  });
});
