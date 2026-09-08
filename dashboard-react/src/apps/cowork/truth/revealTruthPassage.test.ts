import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CoworkPassageHighlighter, PASSAGE_HIGHLIGHT_MS } from "../bridge/CoworkPassageHighlighter";
import { DomReviewAnchorController } from "../bridge/DomReviewAnchorController";
import { CoworkLedgerDecorations, projectCoworkLedgerDecorations, setCoworkEditorLens } from "../editor/ledgerDecorations";
import type { TruthPassageConnection } from "./contracts";
import { revealTruthPassage } from "./revealTruthPassage";

const connection: TruthPassageConnection = {
  expressionId: "expression", spanId: "span", documentId: "doc", documentTitle: "Draft", documentPath: null,
  role: "quote", quote: "Claim passage", selector: { kind: "text_quote", exact: "Claim passage", prefix: "Before. ", suffix: ". After." },
  currentDocument: true, stale: "claim_terminal", claimCanonicalSha256: "canonical", createdAt: "", createdBy: null,
};
let editor: Editor | null = null;
let highlighter: CoworkPassageHighlighter | null = null;
const scrollDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");

afterEach(() => {
  highlighter?.dispose();
  highlighter = null;
  editor?.destroy();
  editor = null;
  vi.useRealTimers();
  if (scrollDescriptor === undefined) Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
  else Object.defineProperty(HTMLElement.prototype, "scrollIntoView", scrollDescriptor);
});

function setup(status: "proposed" | "rejected") {
  const current = new Editor({ element: document.createElement("div"), content: "<p>Before. Claim passage. After.</p>", extensions: [StarterKit.configure({ undoRedo: false }), CoworkLedgerDecorations] });
  editor = current;
  const projection = { edits: [], flags: [], provenance: [], expressions: [{
    expressionId: connection.expressionId, spanId: connection.spanId, quote: connection.quote, quoteAnchor: connection.selector,
    claimRef: "claim", claimStatus: status, isFact: false,
  }], claims: [{ claimId: "claim", expressionId: connection.expressionId, spanId: connection.spanId, quote: connection.quote }] };
  projectCoworkLedgerDecorations(current, projection);
  setCoworkEditorLens(current, "truth");
  const anchors = new DomReviewAnchorController({ getEditor: () => current, getEditorRoot: () => current.view.dom });
  highlighter = new CoworkPassageHighlighter({ getEditor: () => current });
  const show = (target: Parameters<CoworkPassageHighlighter["show"]>[0]) => highlighter!.show(target);
  return { current, projection, anchors, show };
}

describe("explicit Truth passage navigation", () => {
  it("reveals terminal claim prose with one temporary highlight without restoring its persistent mark or changing selection", () => {
    vi.useFakeTimers();
    const { current, projection, anchors, show } = setup("rejected");
    current.commands.setTextSelection({ from: 2, to: 5 });
    const before = current.getJSON();
    const selection = current.state.selection.toJSON();
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scroll });
    expect(current.view.dom.querySelector("[data-wb-expression-id]")).toBeNull();
    expect(revealTruthPassage(current, connection, anchors, show)).toBe(true);
    expect(current.view.dom.querySelector('[data-wb-decoration="passage-highlight"]')).toHaveTextContent("Claim passage");
    expect(scroll).toHaveBeenCalledTimes(1);
    projectCoworkLedgerDecorations(current, projection);
    expect(scroll).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(PASSAGE_HIGHLIGHT_MS);
    expect(current.view.dom.querySelector('[data-wb-decoration="passage-highlight"]')).toBeNull();
    expect(current.view.dom.querySelector("[data-wb-expression-id]")).toBeNull();
    expect(current.getJSON()).toEqual(before);
    expect(current.state.selection.toJSON()).toEqual(selection);
  });

  it("uses an existing expression mark directly without adding a second highlight", () => {
    const { current, anchors } = setup("proposed");
    const reveal = vi.spyOn(anchors, "revealAnchor");
    const show = vi.fn(() => true);
    expect(revealTruthPassage(current, { ...connection, stale: null }, anchors, show)).toBe(true);
    expect(reveal).toHaveBeenCalledExactlyOnceWith("expression", "expression", { flash: true });
    expect(show).not.toHaveBeenCalled();
    anchors.clearFocusedAnchor();
  });

  it("fails closed for a missing or ambiguous selector when no expression mark exists", () => {
    const { current, anchors } = setup("rejected");
    const show = vi.fn(() => true);
    current.commands.setContent("<p>Repeated passage. Repeated passage.</p>");
    expect(revealTruthPassage(current, connection, anchors, show)).toBe(false);
    expect(revealTruthPassage(current, { ...connection, selector: { kind: "text_quote", exact: "Repeated passage", prefix: "", suffix: "" } }, anchors, show)).toBe(false);
    expect(show).not.toHaveBeenCalled();
  });
});
