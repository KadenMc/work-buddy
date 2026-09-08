import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { describe, expect, it, vi } from "vitest";

import {
  CoworkLedgerDecorations,
  projectCoworkLedgerDecorations,
  setCoworkEditorLens,
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
  it("highlights every claim passage and scrolls the first DOM passage on the first offscreen card activation", () => {
    const viewport = document.createElement("div");
    viewport.className = "wb-cowork__editor-region";
    viewport.getBoundingClientRect = () => new DOMRect(20, 100, 500, 300);
    const root = document.createElement("div");
    viewport.append(root);
    const first = ledgerElement("claim-1", "claim");
    const second = ledgerElement("claim-1", "claim");
    // The namespace and alias lookups must share document order.
    second.setAttribute("data-wb-anchor-kind", "claim");
    second.setAttribute("data-wb-anchor-id", "claim-1");
    root.append(first, second);
    first.getClientRects = () => [new DOMRect(30, 10, 150, 20)] as unknown as DOMRectList;
    second.getClientRects = () => [new DOMRect(30, 500, 150, 20)] as unknown as DOMRectList;
    first.scrollIntoView = vi.fn();
    second.scrollIntoView = vi.fn();
    const controller = new DomReviewAnchorController({ getEditorRoot: () => root });
    controller.focusAnchor("claim-1", "claim");
    expect(first).toHaveClass("wb-cowork-anchor--active");
    expect(second).toHaveClass("wb-cowork-anchor--active");
    expect(first.scrollIntoView).not.toHaveBeenCalled();
    controller.revealClaimIfOutsideViewport("claim-1");
    expect(first.scrollIntoView).toHaveBeenCalledExactlyOnceWith({ block: "center", behavior: "smooth" });
    expect(second.scrollIntoView).not.toHaveBeenCalled();
    controller.refresh();
    controller.focusAnchor("claim-1", "claim");
    expect(first.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("keeps scrolling still when any passage intersects the editor viewport, even partly", () => {
    const viewport = document.createElement("div");
    viewport.className = "wb-cowork__editor-region";
    viewport.getBoundingClientRect = () => new DOMRect(20, 100, 500, 300);
    const root = document.createElement("div");
    viewport.append(root);
    const first = ledgerElement("claim-1", "claim");
    const second = ledgerElement("claim-1", "claim");
    root.append(first, second);
    first.getClientRects = () => [new DOMRect(30, 500, 150, 20)] as unknown as DOMRectList;
    second.getClientRects = () => [new DOMRect(30, 399, 150, 20)] as unknown as DOMRectList;
    first.scrollIntoView = vi.fn();
    second.scrollIntoView = vi.fn();
    const controller = new DomReviewAnchorController({ getEditorRoot: () => root });
    controller.revealClaimIfOutsideViewport("claim-1");
    expect(first.scrollIntoView).not.toHaveBeenCalled();
    expect(second.scrollIntoView).not.toHaveBeenCalled();
  });

  it("uses reduced-motion scrolling and never replays a missing card target during refresh", () => {
    const root = document.createElement("div");
    const controller = new DomReviewAnchorController({
      getEditorRoot: () => root,
      windowRef: { innerWidth: 800, innerHeight: 600, matchMedia: () => ({ matches: true }) } as unknown as Window,
    });
    controller.revealClaimIfOutsideViewport("claim-1");
    const mark = ledgerElement("claim-1", "claim");
    mark.getClientRects = () => [new DOMRect(10, 700, 150, 20)] as unknown as DOMRectList;
    mark.scrollIntoView = vi.fn();
    root.append(mark);
    controller.refresh();
    expect(mark.scrollIntoView).not.toHaveBeenCalled();
    controller.revealClaimIfOutsideViewport("claim-1");
    expect(mark.scrollIntoView).toHaveBeenCalledExactlyOnceWith({ block: "center", behavior: "auto" });
  });

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

  it("reveals and visibly focuses a secondary compatible provenance target", () => {
    const editor = new Editor({
      element: document.createElement("div"),
      content: "<p>Shared passage.</p>",
      extensions: [
        StarterKit.configure({ undoRedo: false }),
        CoworkLedgerDecorations,
      ],
    });
    const base = {
      quoteAnchor: {
        exact: "Shared passage",
        prefix: "",
        suffix: ".",
      },
      isDocumentDefault: false,
      authorship: "ai" as const,
      reviewStatus: "not_reviewed" as const,
      currentness: "current" as const,
      resolution: "resolved" as const,
      source: "paste",
      sourceDetail: "Provider: clipboard",
      contributors: "No contributors recorded",
      reviewers: "No reviewers recorded",
      attester: "user-1",
      basis: "user_attestation",
      historyCount: 1,
      effectiveCount: 1,
      recordState: "recorded" as const,
      authorshipFingerprint: "ai",
      reviewFingerprint: "not_reviewed",
      sourceFingerprint: "paste",
    };
    projectCoworkLedgerDecorations(editor, {
      edits: [],
      flags: [],
      expressions: [],
      claims: [],
      provenance: [],
      provenanceOverlay: [
        { ...base, targetId: "primary", recordId: "record-primary" },
        { ...base, targetId: "secondary", recordId: "record-secondary" },
      ],
    });
    setCoworkEditorLens(editor, "provenance");
    const mark = editor.view.dom.querySelector<HTMLElement>(
      "[data-wb-provenance-ids]",
    );
    expect(mark).not.toBeNull();
    const scrollIntoView = vi.fn();
    if (mark !== null) mark.scrollIntoView = scrollIntoView;
    const controller = new DomReviewAnchorController({
      getEditorRoot: () => editor.view.dom,
      getEditor: () => editor,
    });
    controller.attachEditor(editor);

    controller.revealAnchor("secondary", "provenance", { flash: true });

    expect(scrollIntoView).toHaveBeenCalledOnce();
    const focused = editor.view.dom.querySelector<HTMLElement>(
      "[data-wb-provenance-ids]",
    );
    expect(focused).toHaveClass("wb-cowork-anchor--active");
    expect(focused).toHaveClass("wb-cowork-anchor--flash");
    controller.detachEditor();
    editor.destroy();
  });
});
