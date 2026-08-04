import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { describe, expect, it, vi } from "vitest";

import {
  CoworkLedgerDecorations,
  projectCoworkLedgerDecorations,
} from "../editor/ledgerDecorations";
import { DomReviewAnchorController } from "./DomReviewAnchorController";

const legacyProposalElement = (id: string): HTMLElement => {
  const element = document.createElement("ins");
  element.setAttribute("data-id", JSON.stringify(id));
  element.setAttribute("data-wb-suggestion", "insertion");
  return element;
};

const ledgerElement = (
  id: string,
  kind: "proposal" | "claim",
): HTMLElement => {
  const element = document.createElement("span");
  if (kind === "claim") {
    element.setAttribute("data-wb-claim-ids", JSON.stringify([id]));
  } else {
    element.setAttribute("data-wb-anchor-kind", kind);
    element.setAttribute("data-wb-anchor-id", id);
  }
  return element;
};

const createProjectedEditor = (): Editor => {
  const editor = new Editor({
    element: document.createElement("div"),
    content: "<p>Flagged phrase.</p>",
    extensions: [
      StarterKit.configure({ undoRedo: false }),
      CoworkLedgerDecorations,
    ],
  });
  projectCoworkLedgerDecorations(editor, {
    edits: [],
    flags: [
      {
        proposalId: "flag-1",
        quoteAnchor: {
          exact: "Flagged phrase",
          prefix: "",
          suffix: ".",
        },
      },
    ],
    expressions: [],
    claims: [],
    provenance: [],
  });
  return editor;
};

describe("DomReviewAnchorController", () => {
  it("reveals and flashes a legacy proposal anchor on explicit request", () => {
    const editorRoot = document.createElement("div");
    const mark = legacyProposalElement("s1");
    const scrollIntoView = vi.fn();
    mark.scrollIntoView = scrollIntoView;
    editorRoot.append(mark);
    const controller = new DomReviewAnchorController({
      getEditorRoot: () => editorRoot,
    });

    controller.revealAnchor("s1", "proposal", { flash: true });

    expect(scrollIntoView).toHaveBeenCalledOnce();
    expect(mark).toHaveClass("wb-cowork-anchor--active");
    expect(mark).toHaveClass("wb-cowork-anchor--flash");

    // A projection refresh restores durable focus but never replays the old
    // one-shot navigation command (the source of historical snap-back).
    controller.refresh();
    expect(scrollIntoView).toHaveBeenCalledOnce();
    controller.clearFocusedAnchor();
  });

  it("does not replay an unresolved reveal when a later refresh finds the anchor", () => {
    const editorRoot = document.createElement("div");
    const controller = new DomReviewAnchorController({
      getEditorRoot: () => editorRoot,
    });

    controller.revealAnchor("s1", "proposal");
    const mark = legacyProposalElement("s1");
    const scrollIntoView = vi.fn();
    mark.scrollIntoView = scrollIntoView;
    editorRoot.append(mark);

    controller.refresh();

    expect(mark).toHaveClass("wb-cowork-anchor--active");
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("focuses only the requested namespace and clears it explicitly", () => {
    const editorRoot = document.createElement("div");
    const proposal = ledgerElement("same-id", "proposal");
    const claim = ledgerElement("same-id", "claim");
    editorRoot.append(proposal, claim);
    const controller = new DomReviewAnchorController({
      getEditorRoot: () => editorRoot,
    });

    controller.focusAnchor("same-id", "claim");
    expect(claim).toHaveClass("wb-cowork-anchor--active");
    expect(proposal).not.toHaveClass("wb-cowork-anchor--active");

    controller.clearFocusedAnchor();
    expect(claim).not.toHaveClass("wb-cowork-anchor--active");
  });

  it("replays focus requested before mount and after an editor remount", () => {
    let editor: Editor | null = null;
    const controller = new DomReviewAnchorController({
      getEditorRoot: () => editor?.view.dom ?? null,
      getEditor: () => editor,
    });

    controller.focusAnchor("flag-1", "proposal");
    editor = createProjectedEditor();
    controller.attachEditor(editor);
    expect(
      editor.view.dom.querySelector('[data-wb-anchor-id="flag-1"]'),
    ).toHaveClass("wb-cowork-anchor--active");

    controller.detachEditor();
    editor.destroy();
    editor = createProjectedEditor();
    controller.attachEditor(editor);
    expect(
      editor.view.dom.querySelector('[data-wb-anchor-id="flag-1"]'),
    ).toHaveClass("wb-cowork-anchor--active");

    controller.detachEditor();
    editor.destroy();
  });
});
