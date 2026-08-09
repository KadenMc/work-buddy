import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CoworkLedgerDecorations,
  readCoworkLedgerDecorationState,
} from "../editor/ledgerDecorations";
import {
  CoworkPassageHighlighter,
  PASSAGE_HIGHLIGHT_MS,
} from "./CoworkPassageHighlighter";

let editor: Editor | null = null;
let host: HTMLElement | null = null;
let scrollDescriptor: PropertyDescriptor | undefined;

const mountEditor = (): Editor => {
  host = document.createElement("div");
  document.body.append(host);
  editor = new Editor({
    element: host,
    content: "<p>Alpha passage. Beta passage. Gamma passage.</p>",
    extensions: [
      StarterKit.configure({ undoRedo: false }),
      CoworkLedgerDecorations,
    ],
  });
  return editor;
};

afterEach(() => {
  vi.useRealTimers();
  if (scrollDescriptor === undefined) {
    Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
  } else {
    Object.defineProperty(
      HTMLElement.prototype,
      "scrollIntoView",
      scrollDescriptor,
    );
  }
  scrollDescriptor = undefined;
  editor?.destroy();
  editor = null;
  host?.remove();
  host = null;
  document.querySelectorAll("[data-test-external-focus]").forEach((node) => {
    node.remove();
  });
});

describe("CoworkPassageHighlighter", () => {
  it("scrolls and briefly highlights a passage without stealing focus or selection", () => {
    vi.useFakeTimers();
    const current = mountEditor();
    current.commands.setTextSelection({ from: 2, to: 7 });
    const selectionBefore = current.state.selection.toJSON();
    const jsonBefore = current.getJSON();
    const htmlBefore = current.getHTML();

    const externalButton = document.createElement("button");
    externalButton.dataset.testExternalFocus = "true";
    document.body.append(externalButton);
    externalButton.focus();
    expect(document.activeElement).toBe(externalButton);

    const scrollIntoView = vi.fn();
    scrollDescriptor = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollIntoView",
    );
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });

    const highlighter = new CoworkPassageHighlighter({
      getEditor: () => current,
      windowRef: window,
    });
    expect(
      highlighter.show({
        spanId: "span-beta",
        anchor: {
          exact: "Beta passage",
          prefix: "passage. ",
          suffix: ". Gamma",
        },
      }),
    ).toBe(true);

    const visible = current.view.dom.querySelector<HTMLElement>(
      '[data-wb-decoration="passage-highlight"]',
    );
    expect(visible).not.toBeNull();
    expect(visible?.textContent).toBe("Beta passage");
    expect(scrollIntoView).toHaveBeenCalledWith({
      block: "center",
      behavior: "smooth",
    });
    expect(current.state.selection.toJSON()).toEqual(selectionBefore);
    expect(document.activeElement).toBe(externalButton);
    expect(current.getJSON()).toEqual(jsonBefore);
    expect(current.getHTML()).toBe(htmlBefore);

    vi.advanceTimersByTime(PASSAGE_HIGHLIGHT_MS);
    expect(
      current.view.dom.querySelector(
        '[data-wb-decoration="passage-highlight"]',
      ),
    ).toBeNull();
    expect(readCoworkLedgerDecorationState(current)?.highlight).toBeNull();
    expect(current.state.selection.toJSON()).toEqual(selectionBefore);
    expect(document.activeElement).toBe(externalButton);
  });

  it("fails closed when a quote cannot be uniquely resolved", () => {
    const current = mountEditor();
    const highlighter = new CoworkPassageHighlighter({
      getEditor: () => current,
    });
    expect(
      highlighter.show({
        spanId: "missing",
        anchor: { exact: "not in the document" },
      }),
    ).toBe(false);
    expect(readCoworkLedgerDecorationState(current)?.highlight).toBeNull();
  });

  it("passively focuses a staged passage without scrolling until explicitly cleared", () => {
    vi.useFakeTimers();
    const current = mountEditor();
    const scrollIntoView = vi.fn();
    scrollDescriptor = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollIntoView",
    );
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    const highlighter = new CoworkPassageHighlighter({
      getEditor: () => current,
      windowRef: window,
    });

    expect(highlighter.focus({
      spanId: "analysis-candidate-1",
      anchor: {
        exact: "Beta passage",
        prefix: "passage. ",
        suffix: ". Gamma",
      },
    })).toBe(true);
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(readCoworkLedgerDecorationState(current)?.highlight).not.toBeNull();
    expect(highlighter.showAndRefocus({
      spanId: "analysis-candidate-1",
      anchor: {
        exact: "Beta passage",
        prefix: "passage. ",
        suffix: ". Gamma",
      },
    })).toBe(true);
    expect(scrollIntoView).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(PASSAGE_HIGHLIGHT_MS);
    expect(readCoworkLedgerDecorationState(current)?.highlight).not.toBeNull();

    highlighter.clear();
    expect(readCoworkLedgerDecorationState(current)?.highlight).toBeNull();
  });
});
