import { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import { buildEditorExtensions } from "./extensions";
import {
  clearCoworkPassageHighlight,
  readCoworkLedgerDecorationState,
  showCoworkPassageHighlight,
} from "./ledgerDecorations";
import {
  clearCoworkWorkingTarget,
  projectCoworkWorkingTarget,
  projectCoworkWorkingTargetStart,
  readCoworkWorkingTarget,
  readCoworkWorkingTargetStart,
} from "./workingTargetDecorations";
import { resolveQuoteAnchor } from "../suggestions/anchor";

let editor: Editor | null = null;
let host: HTMLElement | null = null;
let collaborativeDocument: Y.Doc | null = null;

const mountEditor = (): Editor => {
  collaborativeDocument = new Y.Doc();
  host = document.createElement("div");
  document.body.append(host);
  editor = new Editor({
    element: host,
    extensions: buildEditorExtensions(collaborativeDocument),
  });
  editor.commands.setContent(
    "<p>Alpha passage. Beta passage. Gamma passage.</p>",
  );
  return editor;
};

afterEach(() => {
  editor?.destroy();
  editor = null;
  collaborativeDocument?.destroy();
  collaborativeDocument = null;
  host?.remove();
  host = null;
});

describe("CoworkWorkingTargetDecorations", () => {
  it("shows one temporary start marker, then replaces it with the exact resolved range", () => {
    const current = mountEditor();
    const beta = resolveQuoteAnchor(current.state.doc, {
      exact: "Beta passage",
      prefix: "",
      suffix: "",
    })!;

    expect(
      projectCoworkWorkingTargetStart(current, {
        position: beta.from,
        label: "Working on start",
      }),
    ).toBe(true);
    expect(readCoworkWorkingTargetStart(current)).toEqual({
      position: beta.from,
      label: "Working on start",
    });
    expect(readCoworkWorkingTarget(current)).toBeNull();
    expect(
      current.view.dom.querySelectorAll(
        '[data-wb-working-target-provisional="true"]',
      ),
    ).toHaveLength(1);
    expect(
      current.view.dom.querySelector(
        '[data-wb-working-target="true"]',
      ),
    ).toBeNull();

    current.view.dispatch(current.state.tr.insertText("Intro ", 1));
    expect(readCoworkWorkingTargetStart(current)).toEqual({
      position: beta.from + 6,
      label: "Working on start",
    });

    expect(
      projectCoworkWorkingTarget(current, {
        from: beta.from + 6,
        to: beta.to + 6,
        label: "Beta passage",
      }),
    ).toBe(true);
    expect(readCoworkWorkingTargetStart(current)).toBeNull();
    expect(readCoworkWorkingTarget(current)).toEqual({
      from: beta.from + 6,
      to: beta.to + 6,
      label: "Beta passage",
    });
    expect(
      current.view.dom.querySelector(
        '[data-wb-working-target-provisional="true"]',
      ),
    ).toBeNull();
    expect(
      current.view.dom.querySelectorAll(
        "[data-wb-working-target-boundary]",
      ),
    ).toHaveLength(2);
  });

  it("renders persistent exact endpoints without entering Yjs or replacing Chat highlighting", () => {
    const current = mountEditor();
    const document = collaborativeDocument!;
    const beta = resolveQuoteAnchor(current.state.doc, {
      exact: "Beta passage",
      prefix: "",
      suffix: "",
    })!;
    const gamma = resolveQuoteAnchor(current.state.doc, {
      exact: "Gamma passage",
      prefix: "",
      suffix: "",
    })!;
    const beforeJson = current.getJSON();
    const beforeHtml = current.getHTML();
    const beforeState = Y.encodeStateVector(document);
    const beforeSnapshot = Y.encodeStateAsUpdate(document);
    let updateCount = 0;
    document.on("update", () => {
      updateCount += 1;
    });

    expect(
      projectCoworkWorkingTarget(current, {
        from: beta.from,
        to: beta.to,
        label: "Beta passage",
      }),
    ).toBe(true);
    expect(
      showCoworkPassageHighlight(current, {
        id: "chat-gamma",
        from: gamma.from,
        to: gamma.to,
      }),
    ).toBe(true);

    expect(
      current.view.dom.querySelector(
        '[data-wb-working-target="true"]',
      ),
    ).toHaveTextContent("Beta passage");
    expect(
      current.view.dom.querySelectorAll(
        "[data-wb-working-target-boundary]",
      ),
    ).toHaveLength(2);
    expect(
      current.view.dom.querySelector(
        '[data-wb-decoration="passage-highlight"]',
      ),
    ).toHaveTextContent("Gamma passage");

    clearCoworkPassageHighlight(current, "chat-gamma");
    expect(readCoworkLedgerDecorationState(current)?.highlight).toBeNull();
    expect(readCoworkWorkingTarget(current)).toEqual({
      from: beta.from,
      to: beta.to,
      label: "Beta passage",
    });
    expect(
      current.view.dom.querySelector(
        '[data-wb-working-target="true"]',
      ),
    ).not.toBeNull();

    expect(updateCount).toBe(0);
    expect(Y.encodeStateVector(document)).toEqual(beforeState);
    expect(Y.encodeStateAsUpdate(document)).toEqual(beforeSnapshot);
    expect(current.getJSON()).toEqual(beforeJson);
    expect(current.getHTML()).toBe(beforeHtml);
  });

  it("maps the dedicated target across edits and clears only its own channel", () => {
    const current = mountEditor();
    const beta = resolveQuoteAnchor(current.state.doc, {
      exact: "Beta passage",
      prefix: "",
      suffix: "",
    })!;
    const gamma = resolveQuoteAnchor(current.state.doc, {
      exact: "Gamma passage",
      prefix: "",
      suffix: "",
    })!;
    projectCoworkWorkingTarget(current, {
      from: beta.from,
      to: beta.to,
      label: "Beta passage",
    });
    showCoworkPassageHighlight(current, {
      id: "chat-gamma",
      from: gamma.from,
      to: gamma.to,
    });

    current.view.dispatch(current.state.tr.insertText("Intro ", 1));
    expect(readCoworkWorkingTarget(current)).toEqual({
      from: beta.from + 6,
      to: beta.to + 6,
      label: "Beta passage",
    });
    expect(
      current.view.dom.querySelector(
        '[data-wb-working-target="true"]',
      ),
    ).toHaveTextContent("Beta passage");

    clearCoworkWorkingTarget(current);
    expect(readCoworkWorkingTarget(current)).toBeNull();
    expect(
      current.view.dom.querySelector(
        "[data-wb-working-target-boundary]",
      ),
    ).toBeNull();
    expect(readCoworkLedgerDecorationState(current)?.highlight?.id).toBe(
      "chat-gamma",
    );
  });
});
