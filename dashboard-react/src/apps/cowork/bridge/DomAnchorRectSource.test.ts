import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

import {
  CoworkLedgerDecorations,
  projectCoworkLedgerDecorations,
} from "../editor/ledgerDecorations";
import { useAlignedStream } from "../rail/useAlignedStream";
import { DomAnchorRectSource } from "./DomAnchorRectSource";

const rect = (top: number, bottom: number): DOMRect =>
  ({
    top,
    bottom,
    height: bottom - top,
    left: 0,
    right: 0,
    width: 0,
    x: 0,
    y: top,
    toJSON: () => ({}),
  }) as DOMRect;

const markElement = (id: string, top: number, bottom: number): HTMLElement => {
  const element = document.createElement("ins");
  element.setAttribute("data-id", JSON.stringify(id));
  element.setAttribute("data-wb-suggestion", "insertion");
  element.getBoundingClientRect = () => rect(top, bottom);
  return element;
};

const ledgerElement = (
  id: string,
  kind: "proposal" | "claim",
  top: number,
  bottom: number,
): HTMLElement => {
  const element = document.createElement("span");
  if (kind === "claim") {
    element.setAttribute("data-wb-claim-ids", JSON.stringify([id]));
  } else {
    element.setAttribute("data-wb-anchor-kind", kind);
    element.setAttribute("data-wb-anchor-id", id);
  }
  element.getBoundingClientRect = () => rect(top, bottom);
  return element;
};

const railRoot = (top: number): HTMLElement => {
  const element = document.createElement("ul");
  element.getBoundingClientRect = () => rect(top, top + 400);
  return element;
};

describe("DomAnchorRectSource", () => {
  it("activates: reports a mark rect in the rail coordinate space", () => {
    const editorRoot = document.createElement("div");
    editorRoot.append(markElement("s1", 120, 140));
    const rail = railRoot(80);

    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => rail,
    });

    // top = markTop(120) - railTop(80) = 40, height = 20.
    expect(source.anchorRect("s1", "proposal")).toEqual({ top: 40, height: 20 });
  });

  it("keeps Review-only ancestor scrolling out of card coordinates", () => {
    const workspace = document.createElement("div");
    const editorRoot = document.createElement("div");
    const mark = markElement("s1", 120, 140);
    editorRoot.append(mark);

    const railScroller = document.createElement("div");
    const rail = railRoot(80);
    let railScrollTop = 0;
    Object.defineProperty(railScroller, "scrollTop", {
      configurable: true,
      get: () => railScrollTop,
    });
    rail.getBoundingClientRect = () =>
      rect(80 - railScrollTop, 480 - railScrollTop);
    railScroller.append(rail);
    workspace.append(editorRoot, railScroller);
    document.body.append(workspace);

    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => rail,
    });

    expect(source.anchorRect("s1", "proposal")).toEqual({
      top: 40,
      height: 20,
    });

    railScrollTop = 60;
    expect(source.anchorRect("s1", "proposal")).toEqual({
      top: 40,
      height: 20,
    });

    workspace.remove();
  });

  it("ignores Review-only scroll events but still reports editor scrolling", () => {
    const workspace = document.createElement("div");
    const editorScroller = document.createElement("div");
    const editorRoot = document.createElement("div");
    const mark = markElement("s1", 120, 140);
    let editorScrollTop = 0;
    mark.getBoundingClientRect = () =>
      rect(120 - editorScrollTop, 140 - editorScrollTop);
    editorRoot.append(mark);
    editorScroller.append(editorRoot);

    const railScroller = document.createElement("div");
    const rail = railRoot(80);
    let railScrollTop = 0;
    Object.defineProperty(railScroller, "scrollTop", {
      configurable: true,
      get: () => railScrollTop,
    });
    rail.getBoundingClientRect = () =>
      rect(80 - railScrollTop, 480 - railScrollTop);
    railScroller.append(rail);
    workspace.append(editorScroller, railScroller);
    document.body.append(workspace);

    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => rail,
    });
    const onChange = vi.fn();
    const unsubscribe = source.subscribe(onChange);

    railScrollTop = 60;
    railScroller.dispatchEvent(new Event("scroll"));
    expect(onChange).not.toHaveBeenCalled();
    expect(source.anchorRect("s1", "proposal")).toEqual({
      top: 40,
      height: 20,
    });

    // A later explicit remeasurement remains stable even though the rail is
    // still scrolled; filtering the scroll event alone would fail this case.
    source.refresh();
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(source.anchorRect("s1", "proposal")).toEqual({
      top: 40,
      height: 20,
    });

    editorScrollTop = 20;
    editorScroller.dispatchEvent(new Event("scroll"));
    expect(onChange).toHaveBeenCalledTimes(2);
    expect(source.anchorRect("s1", "proposal")).toEqual({
      top: 20,
      height: 20,
    });

    unsubscribe();
    workspace.remove();
  });

  it("unions the rects of a multi-node mark", () => {
    const editorRoot = document.createElement("div");
    editorRoot.append(markElement("s1", 100, 120), markElement("s1", 130, 160));
    const rail = railRoot(0);

    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => rail,
    });

    // top = min(100,130) - 0 = 100, height = max(120,160) - min(100,130) = 60.
    expect(source.anchorRect("s1", "proposal")).toEqual({ top: 100, height: 60 });
  });

  it("keeps claim and proposal geometry separate when their raw ids collide", () => {
    const editorRoot = document.createElement("div");
    editorRoot.append(
      ledgerElement("same-id", "proposal", 100, 120),
      ledgerElement("same-id", "claim", 220, 250),
    );
    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => railRoot(0),
    });

    expect(source.anchorRect("same-id", "proposal")).toEqual({
      top: 100,
      height: 20,
    });
    expect(source.anchorRect("same-id", "claim")).toEqual({
      top: 220,
      height: 30,
    });
  });

  it("degrades to null for an unresolved proposal anchor", () => {
    const editorRoot = document.createElement("div");
    editorRoot.append(markElement("s1", 100, 120));
    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => railRoot(0),
    });
    expect(source.anchorRect("f1", "proposal")).toBeNull();
  });

  it("degrades to null when the editor is not mounted", () => {
    const source = new DomAnchorRectSource({
      getEditorRoot: () => null,
      getRailRoot: () => railRoot(0),
    });
    expect(source.anchorRect("s1", "proposal")).toBeNull();
  });

  it("degrades to null when the rail coordinate root is absent", () => {
    const editorRoot = document.createElement("div");
    editorRoot.append(markElement("s1", 100, 120));
    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => null,
    });
    expect(source.anchorRect("s1", "proposal")).toBeNull();
  });

  it("scrolls a mark into view and flashes it on the degrade path", () => {
    const editorRoot = document.createElement("div");
    const mark = markElement("s1", 100, 120);
    const scrollIntoView = vi.fn();
    mark.scrollIntoView = scrollIntoView;
    editorRoot.append(mark);

    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => railRoot(0),
    });
    source.scrollToAnchor("s1");

    expect(scrollIntoView).toHaveBeenCalledOnce();
    expect(mark.classList.contains("wb-cowork-anchor--flash")).toBe(true);
  });

  it("persists focus on only the requested namespace and clears it explicitly", () => {
    const editorRoot = document.createElement("div");
    const proposal = ledgerElement("same-id", "proposal", 100, 120);
    const claim = ledgerElement("same-id", "claim", 220, 250);
    editorRoot.append(proposal, claim);
    const source = new DomAnchorRectSource({
      getEditorRoot: () => editorRoot,
      getRailRoot: () => railRoot(0),
    });

    source.focusAnchor("same-id", "claim");
    expect(claim).toHaveClass("wb-cowork-anchor--active");
    expect(proposal).not.toHaveClass("wb-cowork-anchor--active");

    source.clearFocusedAnchor();
    expect(claim).not.toHaveClass("wb-cowork-anchor--active");
  });

  it("fires a geometry change on resize", () => {
    const source = new DomAnchorRectSource({
      getEditorRoot: () => document.createElement("div"),
      getRailRoot: () => railRoot(0),
    });

    const onChange = vi.fn();
    const unsubscribe = source.subscribe(onChange);

    window.dispatchEvent(new Event("resize"));
    expect(onChange).toHaveBeenCalledTimes(1);

    unsubscribe();
    window.dispatchEvent(new Event("resize"));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("replays pre-mount focus and reports later editor transactions", () => {
    let editor: Editor | null = null;
    const source = new DomAnchorRectSource({
      getEditorRoot: () => editor?.view.dom ?? null,
      getEditor: () => editor,
      getRailRoot: () => railRoot(0),
    });
    const onChange = vi.fn();
    const unsubscribe = source.subscribe(onChange);

    source.focusAnchor("flag-1", "proposal", { scroll: false });
    editor = new Editor({
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

    source.attachEditor(editor);
    expect(
      editor.view.dom.querySelector(
        '[data-wb-anchor-id="flag-1"]',
      ),
    ).toHaveClass("wb-cowork-anchor--active");
    const afterAttach = onChange.mock.calls.length;

    editor.view.dispatch(editor.state.tr.insertText("Intro ", 1));
    expect(onChange.mock.calls.length).toBeGreaterThan(afterAttach);

    source.detachEditor();
    editor.destroy();
    editor = new Editor({
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
    source.attachEditor(editor);
    expect(
      editor.view.dom.querySelector('[data-wb-anchor-id="flag-1"]'),
    ).toHaveClass("wb-cowork-anchor--active");

    source.detachEditor();
    unsubscribe();
    editor.destroy();
  });
});

describe("useAlignedStream activation with the source", () => {
  it("aligns when the source is wired and degrades to normal flow without it", () => {
    const source = new DomAnchorRectSource({
      getEditorRoot: () => null,
      getRailRoot: () => null,
    });
    const withSource = renderHook(() =>
      useAlignedStream({
        anchorRects: source,
        anchors: [{ id: "s1", kind: "proposal" }],
      }),
    );
    expect(withSource.result.current.aligned).toBe(true);

    const withoutSource = renderHook(() =>
      useAlignedStream({
        anchorRects: undefined,
        anchors: [{ id: "s1", kind: "proposal" }],
      }),
    );
    expect(withoutSource.result.current.aligned).toBe(false);
  });
});
