import type { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";

import { WbTrackedChangesAdapterImpl } from "../adapter";
import { editProposal, makeSuggestionEditor, markSummary } from "./support";

/**
 * Code-block suggestion support (SP-1 fork delta 4, the M item). The stock code_block
 * forbids marks, so an agent edit there applied raw with zero marks, a silent gate
 * violation. CoworkCodeBlock admits the three suggestion marks, so a tracked edit inside a
 * fenced code block carries insertion and deletion marks like any other block. The
 * contrast case pins the stock behavior the patch corrects.
 */

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

describe("code-block suggestion tracking", () => {
  it("tracks an edit inside a CoworkCodeBlock as insertion and deletion marks", () => {
    editor = makeSuggestionEditor({
      content: "<pre><code>const x = 1</code></pre>",
      coworkCodeBlock: true,
    });
    const adapter = new WbTrackedChangesAdapterImpl();
    adapter.attach(editor);

    const result = adapter.ingestProposal(
      editProposal("code-1", "1", "2", { prefix: "= " }),
    );
    expect(result.anchored).toBe(true);

    const marks = markSummary(editor);
    const deletion = marks.find((mark) => mark.type === "deletion");
    const insertion = marks.find((mark) => mark.type === "insertion");
    expect(deletion?.text).toBe("1");
    expect(insertion?.text).toBe("2");
    expect(adapter.listOpen()).toEqual(["code-1"]);
  });

  it("shows the stock code_block applies the same edit raw with no marks", () => {
    editor = makeSuggestionEditor({
      content: "<pre><code>const x = 1</code></pre>",
      coworkCodeBlock: false,
    });
    const adapter = new WbTrackedChangesAdapterImpl();
    adapter.attach(editor);

    adapter.ingestProposal(editProposal("code-2", "1", "2", { prefix: "= " }));

    // The stock node forbids marks, so nothing is tracked (SP-1 finding).
    expect(markSummary(editor)).toEqual([]);
    expect(adapter.listOpen()).toEqual([]);
  });
});
