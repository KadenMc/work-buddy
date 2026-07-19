import type { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";

import { resolveQuoteAnchor } from "../suggestions/anchor";
import { makeSuggestionEditor } from "../suggestions/__tests__/support";
import { quoteAnchorFromRange } from "./feedbackAnchor";

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

const rangeOf = (
  ed: Editor,
  exact: string,
  context: { prefix?: string; suffix?: string } = {},
): { from: number; to: number } => {
  const range = resolveQuoteAnchor(ed.state.doc, {
    exact,
    prefix: context.prefix ?? "",
    suffix: context.suffix ?? "",
  });
  if (range === null) throw new Error(`quote not found: ${exact}`);
  return range;
};

describe("quoteAnchorFromRange", () => {
  it("captures the selected quote with bounded prefix and suffix context", () => {
    editor = makeSuggestionEditor({ content: "<p>The quick brown fox jumps</p>" });
    const range = rangeOf(editor, "quick");
    const anchor = quoteAnchorFromRange(editor.state.doc, range.from, range.to);
    expect(anchor).not.toBeNull();
    expect(anchor?.exact).toBe("quick");
    expect(anchor?.prefix).toBe("The ");
    expect(anchor?.suffix.startsWith(" brown")).toBe(true);
  });

  it("round-trips: an anchor built from a range resolves back to that range", () => {
    editor = makeSuggestionEditor({
      content: "<p>set the flag then set the value here</p>",
    });
    // The SECOND "set", ambiguous on the exact quote, disambiguated by context.
    const target = rangeOf(editor, "set", {
      prefix: "flag then ",
      suffix: " the value",
    });
    const anchor = quoteAnchorFromRange(editor.state.doc, target.from, target.to);
    expect(anchor).not.toBeNull();
    expect(resolveQuoteAnchor(editor.state.doc, anchor!)).toEqual(target);
  });

  it("bounds the context window to contextChars on each side", () => {
    editor = makeSuggestionEditor({
      content: `<p>${"a".repeat(50)}TARGET${"b".repeat(50)}</p>`,
    });
    const range = rangeOf(editor, "TARGET");
    const anchor = quoteAnchorFromRange(editor.state.doc, range.from, range.to, 8);
    expect(anchor?.exact).toBe("TARGET");
    expect(anchor?.prefix).toBe("aaaaaaaa");
    expect(anchor?.suffix).toBe("bbbbbbbb");
  });

  it("captures a quote across a block boundary through the inserted newline", () => {
    editor = makeSuggestionEditor({
      content: "<p>first line</p><p>second line</p>",
    });
    const range = rangeOf(editor, "line\nsecond");
    const anchor = quoteAnchorFromRange(editor.state.doc, range.from, range.to);
    expect(anchor?.exact).toBe("line\nsecond");
  });

  it("returns null for a collapsed range", () => {
    editor = makeSuggestionEditor({ content: "<p>text</p>" });
    expect(quoteAnchorFromRange(editor.state.doc, 3, 3)).toBeNull();
  });
});
