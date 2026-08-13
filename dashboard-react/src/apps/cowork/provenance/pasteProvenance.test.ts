import { Editor } from "@tiptap/core";
import { DOMParser as ProseMirrorDOMParser, Slice } from "@tiptap/pm/model";
import { describe, expect, it } from "vitest";
import { ySyncPluginKey } from "@tiptap/y-tiptap";

import { buildSchemaExtensions } from "../editor/extensions";
import {
  COWORK_PROVENANCE_EXACT_MAX_CHARS,
  coworkDirectEntryCaptureFromTransaction,
  coworkPastePassageExcerpt,
  coworkPasteCaptureFromTransaction,
  coworkPasteRangeFromTransaction,
  coworkProvenanceExactWithinLimit,
  isSubstantialCoworkPaste,
  resolveCoworkPasteAnchor,
} from "./pasteProvenance";

const textSlice = (editor: Editor, html: string): Slice => {
  const host = document.createElement("div");
  host.innerHTML = html;
  return ProseMirrorDOMParser.fromSchema(editor.schema).parseSlice(host);
};

describe("paste provenance classification", () => {
  it("does not interrupt a short, simple paragraph", () => {
    const editor = new Editor({ extensions: buildSchemaExtensions() });
    expect(
      isSubstantialCoworkPaste(
        textSlice(editor, "<p>A short sentence.</p>"),
        "A short sentence.",
      ),
    ).toBe(false);
    editor.destroy();
  });

  it.each([
    ["two paragraphs", "<p>One.</p><p>Two.</p>", "One.\n\nTwo."],
    ["a list", "<ul><li><p>One</p></li></ul>", "One"],
    ["a long paragraph", `<p>${"a".repeat(600)}</p>`, "a".repeat(600)],
  ])("asks for %s", (_name, html, text) => {
    const editor = new Editor({ extensions: buildSchemaExtensions() });
    expect(isSubstantialCoworkPaste(textSlice(editor, html), text)).toBe(true);
    editor.destroy();
  });

  it("reads the exact changed range from a paste transaction", () => {
    const editor = new Editor({
      extensions: buildSchemaExtensions(),
      content: "<p>Before after</p>",
    });
    const transaction = editor.state.tr
      .insertText("pasted ", 8)
      .setMeta("uiEvent", "paste");
    expect(coworkPasteRangeFromTransaction(transaction)).toEqual({
      from: 8,
      to: 15,
    });
    editor.destroy();
  });

  it("anchors and classifies the actual normalized inserted span", () => {
    const editor = new Editor({
      extensions: buildSchemaExtensions(),
      content: "<p>Before after</p>",
    });
    const transaction = editor.state.tr
      .insertText("pasted ", 8)
      .setMeta("uiEvent", "paste");

    expect(coworkPasteCaptureFromTransaction(transaction)).toEqual({
      range: { from: 8, to: 15 },
      anchor: {
        exact: "pasted ",
        prefix: "Before ",
        suffix: "after",
      },
      substantial: false,
    });
    editor.destroy();
  });

  it("anchors only the inserted replacement rather than the prior selection", () => {
    const editor = new Editor({
      extensions: buildSchemaExtensions(),
      content: "<p>Before OLD after</p>",
    });
    const transaction = editor.state.tr
      .insertText("new text", 8, 11)
      .setMeta("uiEvent", "paste");

    expect(coworkPasteCaptureFromTransaction(transaction)).toMatchObject({
      range: { from: 8, to: 16 },
      anchor: {
        exact: "new text",
        prefix: "Before ",
        suffix: " after",
      },
    });
    editor.destroy();
  });

  it("requires exact and context to resolve one current passage", () => {
    const editor = new Editor({
      extensions: buildSchemaExtensions(),
      content: "<p>before target after</p><p>before target after</p>",
    });
    expect(
      resolveCoworkPasteAnchor(editor.state.doc, {
        exact: "target",
        prefix: "before ",
        suffix: " after",
      }),
    ).toEqual({ kind: "ambiguous" });
    expect(
      resolveCoworkPasteAnchor(editor.state.doc, {
        exact: "missing",
        prefix: "",
        suffix: "",
      }),
    ).toEqual({ kind: "absent" });
    editor.destroy();
  });

  it("bounds and normalizes the persisted passage excerpt", () => {
    expect(coworkPastePassageExcerpt(`  ${"word ".repeat(100)}`, 24)).toBe(
      "word word word word wor…",
    );
  });

  it("matches the API exact-span boundary in Unicode characters", () => {
    expect(
      coworkProvenanceExactWithinLimit(
        "x".repeat(COWORK_PROVENANCE_EXACT_MAX_CHARS),
      ),
    ).toBe(true);
    expect(
      coworkProvenanceExactWithinLimit(
        "x".repeat(COWORK_PROVENANCE_EXACT_MAX_CHARS + 1),
      ),
    ).toBe(false);
    // JavaScript UTF-16 code units must not halve the server's Python
    // code-point allowance for astral characters.
    expect(
      coworkProvenanceExactWithinLimit(
        "😀".repeat(COWORK_PROVENANCE_EXACT_MAX_CHARS),
      ),
    ).toBe(true);
  });
});

describe("direct-entry provenance capture", () => {
  it("captures the exact text inserted by a local typing transaction", () => {
    const editor = new Editor({
      extensions: buildSchemaExtensions(),
      content: "<p>After</p>",
    });
    const transaction = editor.state.tr.insertText("Test", 1);
    expect(coworkDirectEntryCaptureFromTransaction(transaction)).toEqual({
      range: { from: 1, to: 5 },
      anchor: { exact: "Test", prefix: "", suffix: "After" },
    });
    editor.destroy();
  });

  it("keeps paste, deletion, formatting, history, and applied Yjs work out", () => {
    const editor = new Editor({
      extensions: buildSchemaExtensions(),
      content: "<p>Before</p>",
    });
    expect(
      coworkDirectEntryCaptureFromTransaction(
        editor.state.tr.insertText("paste", 1).setMeta("uiEvent", "paste"),
      ),
    ).toBeNull();
    expect(
      coworkDirectEntryCaptureFromTransaction(editor.state.tr.delete(1, 2)),
    ).toBeNull();
    expect(
      coworkDirectEntryCaptureFromTransaction(
        editor.state.tr.addMark(1, 3, editor.schema.marks.bold!.create()),
      ),
    ).toBeNull();
    expect(
      coworkDirectEntryCaptureFromTransaction(
        editor.state.tr.insertText("undo", 1).setMeta("history$", {}),
      ),
    ).toBeNull();
    expect(
      coworkDirectEntryCaptureFromTransaction(
        editor.state.tr
          .insertText("remote", 1)
          .setMeta(ySyncPluginKey, { isChangeOrigin: true }),
      ),
    ).toBeNull();
    editor.destroy();
  });

  it("never attributes untouched text between disjoint inserted ranges", () => {
    const editor = new Editor({
      extensions: buildSchemaExtensions(),
      content: "<p>alpha omega</p>",
    });
    const transaction = editor.state.tr.insertText("X", 2).insertText("Y", 10);

    expect(coworkDirectEntryCaptureFromTransaction(transaction)).toBeNull();
    editor.destroy();
  });
});
