import type { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";

import { WbTrackedChangesAdapterImpl } from "../adapter";
import {
  WB_ATOM_SUGGESTION_ATTR,
  acceptAtomSuggestion,
  revertAtomSuggestion,
  suggestAtomInsertion,
} from "../atomTracking";
import { makeSuggestionEditor } from "./support";

/**
 * Atom node-attribute tracking (SP-1 fork delta 5). Atoms cannot carry marks, so a
 * suggestion on an atom is tracked as a node attribute. The mechanism, the accept, and the
 * reject are exercised against a known node position, which is how a caller that holds the
 * atom position drives it (automatic quote-anchored routing is deferred to S2).
 */

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

const findNode = (ed: Editor, typeName: string): number => {
  let at = -1;
  ed.state.doc.descendants((node, pos) => {
    if (node.type.name === typeName) at = pos;
    return true;
  });
  return at;
};

const countNodes = (ed: Editor, typeName: string): number => {
  let count = 0;
  ed.state.doc.descendants((node) => {
    if (node.type.name === typeName) count += 1;
    return true;
  });
  return count;
};

const spec = { proposal_id: "atom-1", producer: "model-run-1", epistemic: "ai_proposed" as const };

describe("atom node-attribute tracking through the adapter", () => {
  it("tracks a horizontal rule deletion as a node attribute and lists it open", () => {
    editor = makeSuggestionEditor({
      content: "<p>before</p><hr><p>after</p>",
      coworkAtoms: true,
    });
    const adapter = new WbTrackedChangesAdapterImpl();
    adapter.attach(editor);

    const hrPos = findNode(editor, "horizontalRule");
    const result = adapter.ingestAtomSuggestion(hrPos, "deletion", spec);
    expect(result.anchored).toBe(true);

    const node = editor.state.doc.nodeAt(hrPos);
    expect(node?.attrs[WB_ATOM_SUGGESTION_ATTR]).toMatchObject({
      id: "atom-1",
      type: "deletion",
      producer: "model-run-1",
    });
    expect(adapter.listOpen()).toEqual(["atom-1"]);
  });

  it("accepts a tracked atom deletion by removing the node", () => {
    editor = makeSuggestionEditor({
      content: "<p>before</p><hr><p>after</p>",
      coworkAtoms: true,
    });
    const adapter = new WbTrackedChangesAdapterImpl();
    adapter.attach(editor);
    adapter.ingestAtomSuggestion(findNode(editor, "horizontalRule"), "deletion", spec);

    expect(countNodes(editor, "horizontalRule")).toBe(1);
    adapter.applyDecision({
      proposal_id: "atom-1",
      verb: "confirm",
      canonical_sha256: "c",
    });
    expect(countNodes(editor, "horizontalRule")).toBe(0);
    expect(adapter.listOpen()).toEqual([]);
  });

  it("rejects a tracked atom deletion by keeping the node and clearing the attribute", () => {
    editor = makeSuggestionEditor({
      content: "<p>before</p><hr><p>after</p>",
      coworkAtoms: true,
    });
    const adapter = new WbTrackedChangesAdapterImpl();
    adapter.attach(editor);
    const hrPos = findNode(editor, "horizontalRule");
    adapter.ingestAtomSuggestion(hrPos, "deletion", spec);

    adapter.applyDecision({
      proposal_id: "atom-1",
      verb: "reject_plain",
      canonical_sha256: "c",
    });
    expect(countNodes(editor, "horizontalRule")).toBe(1);
    expect(editor.state.doc.nodeAt(hrPos)?.attrs[WB_ATOM_SUGGESTION_ATTR]).toBeNull();
  });
});

describe("atom insertion tracking commands", () => {
  it("keeps an accepted insertion node and drops a reverted one", () => {
    editor = makeSuggestionEditor({
      content: "<p>before</p><hr><p>after</p>",
      coworkAtoms: true,
    });
    const hrPos = findNode(editor, "horizontalRule");

    // Track the rule as a proposed insertion.
    suggestAtomInsertion(hrPos, spec)(editor.state, editor.view.dispatch);
    expect(editor.state.doc.nodeAt(hrPos)?.attrs[WB_ATOM_SUGGESTION_ATTR]).toMatchObject({
      type: "insertion",
    });

    // Accepting an insertion keeps the node and clears the attribute.
    acceptAtomSuggestion("atom-1")(editor.state, editor.view.dispatch);
    expect(countNodes(editor, "horizontalRule")).toBe(1);
    expect(findNodeAttr(editor, hrPos)).toBeNull();

    // Re-track and revert: an insertion revert removes the node.
    suggestAtomInsertion(findNode(editor, "horizontalRule"), spec)(
      editor.state,
      editor.view.dispatch,
    );
    revertAtomSuggestion("atom-1")(editor.state, editor.view.dispatch);
    expect(countNodes(editor, "horizontalRule")).toBe(0);
  });
});

const findNodeAttr = (ed: Editor, pos: number): unknown =>
  ed.state.doc.nodeAt(pos)?.attrs[WB_ATOM_SUGGESTION_ATTR] ?? null;
