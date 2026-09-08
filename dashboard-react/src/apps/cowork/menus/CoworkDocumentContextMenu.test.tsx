import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { expectNoAccessibilityViolations } from "../../../test/setup";
import { CoworkLedgerDecorations, projectCoworkLedgerDecorations, setCoworkEditorLens } from "../editor/ledgerDecorations";
import { DEFAULT_COWORK_SHORTCUT_BINDINGS } from "../keyboard";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import { TruthStore } from "../truth/store";
import { InMemoryCoworkFeedbackTransport } from "../feedback/feedbackClient";
import { CoworkDocumentContextMenu, type CoworkDocumentContextMenuProps } from "./CoworkDocumentContextMenu";

let editor: Editor | null = null;
let host: HTMLElement | null = null;
afterEach(() => { editor?.destroy(); editor = null; host?.remove(); host = null; vi.restoreAllMocks(); });
const mount = (overrides: Partial<CoworkDocumentContextMenuProps> = {}) => {
  host = document.createElement("div");
  document.body.append(host);
  editor = new Editor({ element: host, content: "<p>First claim appears here.</p><p>Another passage appears here.</p>",
    extensions: [StarterKit.configure({ undoRedo: false }), CoworkLedgerDecorations] });
  const ed = editor;
  vi.spyOn(ed.view, "coordsAtPos").mockReturnValue({ left: 20, right: 40, top: 20, bottom: 40 });
  vi.spyOn(ed.view, "posAtCoords").mockReturnValue({ pos: 4, inside: 0 });
  const onAsk = vi.fn();
  const result = render(<CoworkDocumentContextMenu editor={ed} activeLens="neutral" actions={{ onAsk }} {...overrides} />);
  return { ed, onAsk, ...result };
};
const select = (ed: Editor, exact: string) => {
  const range = resolveQuoteAnchor(ed.state.doc, { exact, prefix: "", suffix: "" });
  if (!range) throw new Error("Test passage missing.");
  act(() => { ed.commands.setTextSelection(range); });
};
const contextMenu = (ed: Editor) => fireEvent.contextMenu(ed.view.dom, { clientX: 20, clientY: 25 });

describe("CoworkDocumentContextMenu", () => {
  it.each([{ key: "ContextMenu" }, { key: "F10", shiftKey: true }])("opens from the editor keyboard and restores editor focus on Escape ($key)", async (key) => {
    const { ed } = mount();
    ed.view.focus();
    fireEvent.keyDown(ed.view.dom, key);
    const menu = await screen.findByRole("menu", { name: "Actions for this passage" });
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    await waitFor(() => expect(ed.view.dom).toHaveFocus());
  });

  it("opens from right-click and uses the frozen selection for Ask", async () => {
    const onAsk = vi.fn();
    const { ed } = mount({ actions: { onAsk } });
    select(ed, "First claim");
    contextMenu(ed);
    await screen.findByRole("menu");
    select(ed, "Another passage");
    await userEvent.click(screen.getByRole("menuitem", { name: /^Ask about this part/ }));
    await waitFor(() => expect(onAsk).toHaveBeenCalledOnce());
    expect(ed.state.doc.textBetween(ed.state.selection.from, ed.state.selection.to)).toBe("First claim");
  });

  it("anchors the selection menu to its visible handle when the passage is above the viewport", async () => {
    const onOpenClaim = vi.fn();
    const { ed } = mount({ activeLens: "truth", actions: { onOpenClaim } });
    act(() => {
      projectCoworkLedgerDecorations(ed, { edits: [], flags: [], provenance: [],
        expressions: [{ expressionId: "x", spanId: "s", quote: "First claim", claimRef: "claim", claimStatus: "proposed", proposition: "Exact proposition" }],
        claims: [{ claimId: "claim", expressionId: "x", spanId: "s", quote: "First claim" }] });
      setCoworkEditorLens(ed, "truth");
    });
    vi.mocked(ed.view.coordsAtPos).mockReturnValue({ left: 66, right: 80, top: -550, bottom: -530 });
    select(ed, "First claim");
    const before = ed.getJSON();
    const selection = ed.state.selection.toJSON();
    const handle = await screen.findByRole("button", { name: "Passage actions" });
    vi.spyOn(handle, "getBoundingClientRect").mockReturnValue(new DOMRect(170, 8, 120, 36));

    await userEvent.click(handle);

    await screen.findByRole("menu", { name: "Actions for this passage" });
    const anchor = document.querySelector<HTMLElement>(".wb-cowork-menu-anchor");
    expect(anchor).toHaveStyle({ left: "170px", top: "44px" });
    await userEvent.click(screen.getByRole("menuitem", { name: /^Show claim/ }));
    await waitFor(() => expect(onOpenClaim).toHaveBeenCalledExactlyOnceWith("claim"));
    expect(ed.getJSON()).toEqual(before);
    expect(ed.state.selection.toJSON()).toEqual(selection);
  });

  it.each([
    { left: -300, right: -280, top: -550, bottom: -530 },
    { left: 5000, right: 5020, top: 6000, bottom: 6020 },
  ])("keeps keyboard menu anchors inside the viewport when editor coordinates are offscreen ($top)", async (coordinates) => {
    const { ed } = mount();
    vi.mocked(ed.view.coordsAtPos).mockReturnValue(coordinates);
    ed.view.focus();

    fireEvent.keyDown(ed.view.dom, { key: "F10", shiftKey: true });

    await screen.findByRole("menu", { name: "Actions for this passage" });
    const anchor = document.querySelector<HTMLElement>(".wb-cowork-menu-anchor");
    expect(anchor).not.toBeNull();
    expect(Number.parseFloat(anchor!.style.left)).toBeGreaterThanOrEqual(0);
    expect(Number.parseFloat(anchor!.style.left)).toBeLessThan(window.innerWidth);
    expect(Number.parseFloat(anchor!.style.top)).toBeGreaterThanOrEqual(0);
    expect(Number.parseFloat(anchor!.style.top)).toBeLessThan(window.innerHeight);
  });

  it.each([
    { binding: "Alt+Enter", modifier: { altKey: true } },
    { binding: "Shift+Enter", modifier: { shiftKey: true } },
  ])("opens the caret's claim with $binding before Tiptap changes the neutral document", ({ binding, modifier }) => {
    const onOpenClaim = vi.fn();
    const { ed } = mount({ activeLens: "neutral", actions: {
      onOpenClaim,
      shortcutBindings: { ...DEFAULT_COWORK_SHORTCUT_BINDINGS, openClaim: binding },
    } });
    act(() => {
      projectCoworkLedgerDecorations(ed, {
        edits: [], flags: [], provenance: [],
        expressions: [{ expressionId: "x", spanId: "s", quote: "First claim", claimRef: "claim", claimStatus: "proposed", proposition: "Exact proposition" }],
        claims: [{ claimId: "claim", expressionId: "x", spanId: "s", quote: "First claim" }],
      });
      setCoworkEditorLens(ed, "neutral");
      ed.commands.setTextSelection(4);
      ed.view.focus();
    });
    const before = ed.getJSON();
    const from = ed.state.selection.from;
    expect(ed.state.selection.empty).toBe(true);
    expect(ed.view.dom.querySelector("[data-wb-anchor-kind='expression']")).toBeNull();

    fireEvent.keyDown(ed.view.dom, { key: "Enter", code: "Enter", keyCode: 13, ...modifier });

    expect(onOpenClaim).toHaveBeenCalledExactlyOnceWith("claim");
    expect(ed.getJSON()).toEqual(before);
    expect(ed.state.selection.empty).toBe(true);
    expect(ed.state.selection.from).toBe(from);
    expect(ed.view.dom.querySelector("br:not(.ProseMirror-trailingBreak)")).toBeNull();
  });

  it("leaves a configured Shift+Enter to Tiptap when the caret has no claim", () => {
    const onOpenClaim = vi.fn();
    const { ed } = mount({ actions: {
      onOpenClaim,
      shortcutBindings: { ...DEFAULT_COWORK_SHORTCUT_BINDINGS, openClaim: "Shift+Enter" },
    } });
    act(() => {
      ed.commands.setTextSelection(4);
      ed.view.focus();
    });
    const before = ed.getJSON();

    fireEvent.keyDown(ed.view.dom, { key: "Enter", code: "Enter", keyCode: 13, shiftKey: true });

    expect(onOpenClaim).not.toHaveBeenCalled();
    expect(ed.getJSON()).not.toEqual(before);
    expect(ed.view.dom.querySelector("br:not(.ProseMirror-trailingBreak)")).not.toBeNull();
  });

  it("leaves the native menu alone when the pointer cannot be placed", () => {
    const { ed } = mount();
    vi.mocked(ed.view.posAtCoords).mockReturnValue(null);
    const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    ed.view.dom.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("hides the selection handle and closes the menu when its editor becomes inactive", async () => {
    const onAsk = vi.fn();
    const { ed, rerender } = mount({ actions: { active: false, onAsk } });
    select(ed, "First claim");
    contextMenu(ed);
    expect(screen.queryByRole("button", { name: "Passage actions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    rerender(<CoworkDocumentContextMenu editor={ed} activeLens="neutral" actions={{ active: true, onAsk }} />);
    await userEvent.click(await screen.findByRole("button", { name: "Passage actions" }));
    await screen.findByRole("menu");
    rerender(<CoworkDocumentContextMenu editor={ed} activeLens="neutral" actions={{ active: false, onAsk }} />);
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Passage actions" })).not.toBeInTheDocument();
    expect(onAsk).not.toHaveBeenCalled();
  });

  it("opens through the selection handle and freezes a change request's passage", async () => {
    const transport = new InMemoryCoworkFeedbackTransport();
    const onFeedbackCaptured = vi.fn();
    const { ed } = mount({ documentId: "doc", storeId: "store", feedbackTransport: transport, onFeedbackCaptured });
    select(ed, "First claim");
    await userEvent.click(await screen.findByRole("button", { name: "Passage actions" }));
    await screen.findByRole("menu");
    select(ed, "Another passage");
    await userEvent.click(screen.getByRole("menuitem", { name: /^Request a change here/ }));
    const dialog = await screen.findByRole("dialog", { name: "Request a change here" });
    expect(within(dialog).getByLabelText("Selected passage")).toHaveTextContent("First claim");
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Change request" }), "  Use the measured value.  ");
    await userEvent.click(within(dialog).getByRole("button", { name: "Send change request" }));
    await waitFor(() => expect(onFeedbackCaptured).toHaveBeenCalledOnce());
    expect(transport.lastRequest?.span.exact).toBe("First claim");
    expect(transport.lastRequest?.text).toBe("  Use the measured value.  ");
  });

  it("preserves an open request draft and guards submission when the latest policy blocks sending", async () => {
    const user = userEvent.setup();
    const transport = new InMemoryCoworkFeedbackTransport();
    const submit = vi.spyOn(transport, "submit");
    const onFeedbackCaptured = vi.fn();
    const { ed, rerender } = mount({ documentId: "doc", storeId: "store", feedbackTransport: transport, onFeedbackCaptured });
    select(ed, "First claim");
    contextMenu(ed);
    await user.click(await screen.findByRole("menuitem", { name: /^Request a change here/ }));
    const input = screen.getByRole("textbox", { name: "Change request" });
    await user.type(input, "  Keep this exact draft.  ");
    const view = (readOnly: boolean, active: boolean, feedback: boolean) => <CoworkDocumentContextMenu
      editor={ed} activeLens="neutral" documentId="doc" storeId="store" readOnly={readOnly} active={active}
      feedbackTransport={transport} onFeedbackCaptured={feedback ? onFeedbackCaptured : undefined} />;
    const assertBlocked = (reason: string) => {
      expect(input).toHaveValue("  Keep this exact draft.  ");
      expect(screen.getByRole("alert")).toHaveTextContent(reason);
      expect(screen.getByRole("button", { name: "Send change request" })).toBeDisabled();
      const form = input.closest("form");
      if (form === null) throw new Error("Request form missing.");
      fireEvent.submit(form);
      expect(submit).not.toHaveBeenCalled();
    };

    rerender(view(true, true, true));
    assertBlocked("This document is read-only.");
    rerender(view(false, false, true));
    assertBlocked("Return to this document before sending.");
    rerender(view(false, true, false));
    assertBlocked("Change requests are unavailable for this document.");

    rerender(view(false, true, true));
    expect(input).toHaveValue("  Keep this exact draft.  ");
    await user.click(screen.getByRole("button", { name: "Send change request" }));
    await waitFor(() => expect(onFeedbackCaptured).toHaveBeenCalledOnce());
    expect(submit).toHaveBeenCalledOnce();
    expect(transport.lastRequest?.text).toBe("  Keep this exact draft.  ");
    expect(transport.lastRequest?.span.exact).toBe("First claim");
  });

  it("keeps claim inspection enabled and decisions visibly blocked in read-only mode", async () => {
    const onOpenClaim = vi.fn();
    const truthStore = new TruthStore();
    const { ed } = mount({ activeLens: "truth", readOnly: true, actions: { truthStore, onOpenClaim } });
    act(() => {
      projectCoworkLedgerDecorations(ed, { edits: [], flags: [], provenance: [],
        expressions: [{ expressionId: "x", spanId: "s", quote: "First claim", claimRef: "claim", claimStatus: "proposed", proposition: "Exact proposition" }],
        claims: [{ claimId: "claim", expressionId: "x", spanId: "s", quote: "First claim" }] });
      setCoworkEditorLens(ed, "truth");
    });
    const before = ed.getJSON();
    contextMenu(ed);
    await screen.findByRole("menu");
    expect(screen.getByRole("menuitem", { name: /^Confirm claim/ })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("menuitem", { name: /^Reject claim/ })).toHaveTextContent("This document is read-only.");
    await userEvent.click(screen.getByRole("menuitem", { name: /^Show claim/ }));
    await waitFor(() => expect(onOpenClaim).toHaveBeenCalledWith("claim"));
    expect(truthStore.getState().requestedDecision).toBeNull();
    expect(ed.getJSON()).toEqual(before);
  });

  it("dismisses a stale target when prose changes while its menu is open", async () => {
    const { ed, onAsk } = mount();
    contextMenu(ed);
    await screen.findByRole("menu");
    act(() => { ed.commands.insertContent("Changed "); });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(onAsk).not.toHaveBeenCalled();
  });

  it("gives disabled menu items descriptions and clears automated accessibility checks", async () => {
    const { ed } = mount({ activeLens: "truth", readOnly: true });
    select(ed, "First claim");
    contextMenu(ed);
    const menu = await screen.findByRole("menu");
    const analyze = screen.getByRole("menuitem", { name: /^Analyze passage/ });
    expect(analyze).toHaveAttribute("aria-disabled", "true");
    expect(analyze).toHaveAccessibleDescription(/This document is read-only/);
    await expectNoAccessibilityViolations(menu);
  });
});
