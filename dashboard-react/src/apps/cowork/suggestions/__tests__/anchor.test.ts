import type { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";

import { resolveQuoteAnchor } from "../anchor";
import { makeSuggestionEditor } from "./support";

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

const textOf = (ed: Editor, range: { from: number; to: number }): string =>
  ed.state.doc.textBetween(range.from, range.to);

describe("resolveQuoteAnchor", () => {
  it("resolves a unique quote to the exact ProseMirror range", () => {
    editor = makeSuggestionEditor({ content: "<p>The quick brown fox</p>" });
    const range = resolveQuoteAnchor(editor.state.doc, {
      exact: "quick",
      prefix: "The ",
      suffix: " brown",
    });
    expect(range).not.toBeNull();
    expect(textOf(editor, range as { from: number; to: number })).toBe("quick");
  });

  it("disambiguates repeated quotes by the prefix and suffix context", () => {
    editor = makeSuggestionEditor({ content: "<p>set the flag then set the value</p>" });
    const range = resolveQuoteAnchor(editor.state.doc, {
      exact: "set",
      prefix: "flag then ",
      suffix: " the value",
    });
    expect(range).not.toBeNull();
    // The second "set" precedes " the value".
    const resolved = range as { from: number; to: number };
    expect(textOf(editor, resolved)).toBe("set");
    expect(editor.state.doc.textBetween(resolved.to, resolved.to + 10)).toBe(" the value");
  });

  it("returns null when repeated quotes cannot be disambiguated", () => {
    editor = makeSuggestionEditor({ content: "<p>na na na na</p>" });
    const range = resolveQuoteAnchor(editor.state.doc, {
      exact: "na",
      prefix: "",
      suffix: "",
    });
    expect(range).toBeNull();
  });

  it("returns null for an absent quote", () => {
    editor = makeSuggestionEditor({ content: "<p>The quick brown fox</p>" });
    expect(
      resolveQuoteAnchor(editor.state.doc, { exact: "elephant", prefix: "", suffix: "" }),
    ).toBeNull();
  });

  it("returns null for an empty quote", () => {
    editor = makeSuggestionEditor({ content: "<p>The quick brown fox</p>" });
    expect(
      resolveQuoteAnchor(editor.state.doc, { exact: "", prefix: "The ", suffix: "" }),
    ).toBeNull();
  });

  it("matches a quote that spans a block boundary through the inserted newline", () => {
    editor = makeSuggestionEditor({ content: "<p>first line</p><p>second line</p>" });
    const range = resolveQuoteAnchor(editor.state.doc, {
      exact: "line\nsecond",
      prefix: "first ",
      suffix: " line",
    });
    expect(range).not.toBeNull();
  });
});
