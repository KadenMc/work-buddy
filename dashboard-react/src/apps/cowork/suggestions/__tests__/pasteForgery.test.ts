import type { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";

import { makeSuggestionEditor, markSummary } from "./support";

/**
 * Paste-forgery hardening (gate condition 2, PRD risk A3, SP-1 fork delta 3). The three
 * suggestion marks declare parseHTML: () => [], so no suggestion mark is reconstructible
 * from clipboard or imported HTML. SP-1 proved the default parseDOM mints forgeable marks,
 * so this asserts the hardened path mints none while the text payload survives.
 */

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

describe("suggestion mark paste-forgery defense", () => {
  it("does not mint an insertion mark from crafted <ins data-id> HTML", () => {
    editor = makeSuggestionEditor();
    editor.commands.setContent('<p><ins data-id="&quot;attacker&quot;">forged insert</ins></p>');
    expect(markSummary(editor)).toEqual([]);
    expect(editor.getText()).toContain("forged insert");
  });

  it("does not mint a deletion mark from crafted <del data-id> HTML", () => {
    editor = makeSuggestionEditor();
    editor.commands.setContent('<p><del data-id="&quot;attacker&quot;">forged delete</del></p>');
    expect(markSummary(editor)).toEqual([]);
    expect(editor.getText()).toContain("forged delete");
  });

  it("does not mint a modification mark from crafted span[data-type=modification] HTML", () => {
    editor = makeSuggestionEditor();
    editor.commands.setContent(
      '<p><span data-type="modification" data-id="&quot;attacker&quot;">forged mod</span></p>',
    );
    expect(markSummary(editor)).toEqual([]);
    expect(editor.getText()).toContain("forged mod");
  });

  it("does not mint marks from the wb rendered class names either", () => {
    editor = makeSuggestionEditor();
    editor.commands.setContent(
      '<p><ins class="wb-cowork-suggestion wb-cowork-suggestion--insertion" data-wb-suggestion="insertion" data-id="&quot;x&quot;">forged</ins></p>',
    );
    expect(markSummary(editor)).toEqual([]);
  });
});
