import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CoworkLedgerDecorations, projectCoworkLedgerDecorations } from "../editor/ledgerDecorations";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import { TruthStore, type TruthActionController } from "../truth/store";
import type { TruthEditorIntegration, TruthSelectionCapture } from "../truth/contracts";
import { coworkTruthMenuItems, resolveCoworkPassageTarget, selectCoworkPassageTarget } from "./documentActions";

let editor: Editor | null = null;
afterEach(() => { editor?.destroy(); editor = null; });
const mount = () => editor = new Editor({
  content: "<p>First claim appears here.</p><p>Another passage appears here.</p>",
  extensions: [StarterKit.configure({ undoRedo: false }), CoworkLedgerDecorations],
});
const range = (ed: Editor, exact: string) => {
  const found = resolveQuoteAnchor(ed.state.doc, { exact, prefix: "", suffix: "" });
  if (found === null) throw new Error("Test passage was not found.");
  return found;
};
const capture: TruthSelectionCapture = {
  schema: "wb.cowork.truth-selection/v1", captureId: "capture", storeId: "store", documentId: "doc",
  structuredHeadSha256: "head", ydocGenerationSha256: "generation", projectionSha256: "projection",
  label: "Selection", wordCount: 2,
  selector: { kind: "text_quote", exact: "First claim", prefix: "", suffix: " appears here.", start: 0, end: 11 },
};
const integration = (): TruthEditorIntegration => ({
  captureSelection: vi.fn(async () => capture), revealPassage: vi.fn(), focusClaim: vi.fn(),
});
const controller = (blocked: string | null = null): TruthActionController => ({
  analyzeBlockedReason: blocked, manualBlockedReason: blocked, analyzePassage: vi.fn(), openComposer: vi.fn(),
});

describe("document passage targets", () => {
  it("uses the current selection only when the pointer falls inside it", () => {
    const ed = mount();
    const first = range(ed, "First claim");
    const other = range(ed, "Another passage");
    ed.commands.setTextSelection(first);
    expect(resolveCoworkPassageTarget(ed, first.from + 1)?.anchor.exact).toBe("First claim");
    const pointed = resolveCoworkPassageTarget(ed, other.from + 1);
    expect(pointed?.anchor.exact).toBe("Another passage appears here.");
    expect(pointed?.selected).toBe(false);
    expect(resolveCoworkPassageTarget(ed)?.anchor.exact).toBe("First claim");
  });

  it("does not turn a root boundary or invalid pointer position into the whole document", () => {
    const ed = mount();
    expect(resolveCoworkPassageTarget(ed, 0)).toBeNull();
    expect(resolveCoworkPassageTarget(ed, Number.NaN)).toBeNull();
  });

  it("freezes a passage across selection changes and refuses it after a prose edit", () => {
    const ed = mount();
    ed.commands.setTextSelection(range(ed, "First claim"));
    const target = resolveCoworkPassageTarget(ed)!;
    ed.commands.setTextSelection(range(ed, "Another passage"));
    selectCoworkPassageTarget(ed, target);
    expect(ed.state.doc.textBetween(ed.state.selection.from, ed.state.selection.to)).toBe("First claim");
    ed.commands.insertContent("Changed claim");
    expect(() => selectCoworkPassageTarget(ed, target)).toThrow("The passage changed.");
  });

  it("discovers overlapping claims and proposals without adding a lens overlay", () => {
    const ed = mount();
    projectCoworkLedgerDecorations(ed, {
      edits: [], provenance: [], flags: [{ proposalId: "suggestion", quoteAnchor: { exact: "claim appears", prefix: "", suffix: "" } }],
      expressions: ["a", "b"].map((id) => ({ expressionId: id, spanId: id, quote: "First claim", claimRef: id,
        claimStatus: "proposed", proposition: `Proposition ${id}` })),
      claims: ["a", "b"].map((id) => ({ claimId: id, expressionId: id, spanId: id, quote: "First claim" })),
    });
    const target = resolveCoworkPassageTarget(ed, range(ed, "claim").from + 1)!;
    expect(target.claims.map((claim) => claim.id)).toEqual(["a", "b"]);
    expect(target.proposals).toEqual(["suggestion"]);
    expect(ed.view.dom.querySelector("[data-wb-anchor-kind='expression']")).toBeNull();
  });
});

describe("shared Truth actions", () => {
  const setup = () => {
    const ed = mount();
    ed.commands.setTextSelection(range(ed, "First claim"));
    const target = resolveCoworkPassageTarget(ed)!;
    const truthStore = new TruthStore();
    const actions = controller();
    truthStore.registerActions(actions);
    const truthEditor = integration();
    const prepare = vi.fn(() => selectCoworkPassageTarget(ed, target));
    return { ed, target, truthStore, actions, truthEditor, prepare };
  };

  it("uses the shared composer and the frozen passage capture", async () => {
    const { ed, target, truthStore, actions, truthEditor, prepare } = setup();
    const item = coworkTruthMenuItems("truth", target, { truthStore, truthEditor }, prepare).find((item) => item.id === "propose")!;
    ed.commands.setTextSelection(range(ed, "Another passage"));
    await item.run();
    expect(prepare).toHaveBeenCalledOnce();
    expect(truthEditor.captureSelection).toHaveBeenCalledOnce();
    expect(actions.openComposer).toHaveBeenCalledWith("propose", capture);
  });

  it("rechecks a shared action guard when permission changes while the menu is open", async () => {
    const { target, truthStore, actions, truthEditor, prepare } = setup();
    const item = coworkTruthMenuItems("truth", target, { truthStore, truthEditor }, prepare).find((item) => item.id === "propose")!;
    truthStore.registerActions(controller("Adding claims is unavailable."));
    await expect(item.run()).rejects.toThrow("Adding claims is unavailable.");
    expect(truthEditor.captureSelection).not.toHaveBeenCalled();
    expect(actions.openComposer).not.toHaveBeenCalled();
  });

  it("rechecks the shared guard after an asynchronous capture", async () => {
    const { target, truthStore, actions, truthEditor, prepare } = setup();
    let finish!: (value: TruthSelectionCapture) => void;
    const captureSelection = vi.fn(() => new Promise<TruthSelectionCapture>((resolve) => { finish = resolve; }));
    const item = coworkTruthMenuItems("truth", target, { truthStore, truthEditor: { ...truthEditor, captureSelection } }, prepare).find((item) => item.id === "connect")!;
    const pending = item.run();
    truthStore.registerActions(controller("Return to the claims list first."));
    finish(capture);
    await expect(pending).rejects.toThrow("Return to the claims list first.");
    expect(actions.openComposer).not.toHaveBeenCalled();
  });

  it("keeps read-only mutation items visible and unavailable", async () => {
    const { target, truthStore, truthEditor, prepare } = setup();
    const items = coworkTruthMenuItems("truth", target, { truthStore, truthEditor }, prepare, { readOnly: true });
    expect(items.map((item) => item.label)).toEqual(["Analyze passage", "Connect to existing claim…", "Add claim…"]);
    for (const item of items) {
      expect(item.blockedReason).toBe("This document is read-only.");
      await expect(item.run()).rejects.toThrow("read-only");
    }
    expect(prepare).not.toHaveBeenCalled();
  });

  it("opens a claim decision in the rail without performing a ledger mutation", async () => {
    const { target, truthStore, prepare } = setup();
    const onOpenClaim = vi.fn();
    const items = coworkTruthMenuItems("truth", { ...target, claims: [{ id: "claim", proposition: "Exact claim", status: "proposed" }] },
      { truthStore, onOpenClaim }, prepare);
    await items.find((item) => item.id === "confirm:claim")!.run();
    expect(onOpenClaim).toHaveBeenCalledWith("claim");
    expect(truthStore.getState().requestedDecision).toMatchObject({ claimId: "claim", action: "confirm" });
    expect(prepare).not.toHaveBeenCalled();
  });

  it("offers on-demand claim history in other lenses without decision verbs", () => {
    const { target, truthStore, prepare } = setup();
    const items = coworkTruthMenuItems("neutral", { ...target, claims: [{ id: "claim", proposition: "Exact claim", status: "rejected" }] },
      { truthStore, onOpenClaim: vi.fn() }, prepare);
    expect(items.map((item) => item.label)).toEqual(["Show claim"]);
  });
});
