import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import { buildEditorExtensions } from "./extensions";
import {
  clearCoworkLedgerAnchorFocus,
  CoworkLedgerDecorations,
  focusCoworkLedgerAnchor,
  projectCoworkLedgerDecorations,
  readCoworkLedgerDecorationState,
  setCoworkEditorLens,
} from "./ledgerDecorations";

const CONTENT =
  "<p>A flagged phrase. An expression phrase. A confirmed phrase. A human phrase.</p>";

let editor: Editor | null = null;
let host: HTMLElement | null = null;

const mountEditor = (content = CONTENT): Editor => {
  host = document.createElement("div");
  document.body.append(host);
  editor = new Editor({
    element: host,
    content,
    extensions: [
      StarterKit.configure({ undoRedo: false }),
      CoworkLedgerDecorations,
    ],
  });
  return editor;
};

afterEach(() => {
  editor?.destroy();
  editor = null;
  host?.remove();
  host = null;
});

describe("CoworkLedgerDecorations", () => {
  it("switches lenses without changing prose or the editor selection", () => {
    const current = mountEditor();
    current.commands.setTextSelection({ from: 3, to: 9 });
    const beforeJson = current.getJSON();
    const beforeSelection = current.state.selection.toJSON();
    projectCoworkLedgerDecorations(current, {
      edits: [],
      flags: [
        {
          proposalId: "lens-flag",
          quoteAnchor: { exact: "flagged phrase", prefix: "A ", suffix: ". An" },
        },
      ],
      expressions: [
        {
          expressionId: "lens-expression",
          spanId: "lens-span",
          quote: "expression phrase",
          claimRef: "lens-claim",
          claimStatus: "confirmed",
        },
      ],
      claims: [],
      provenance: [],
    });

    expect(current.view.dom.querySelector('[data-wb-decoration="flag"]')).not.toBeNull();
    setCoworkEditorLens(current, "truth");
    expect(current.view.dom.querySelector('[data-wb-decoration="flag"]')).toBeNull();
    expect(current.view.dom.querySelector('[data-wb-decoration="expression"]')).not.toBeNull();
    setCoworkEditorLens(current, "neutral");
    expect(current.view.dom.querySelector(".wb-cowork-ledger-decoration")).toBeNull();
    expect(current.getJSON()).toEqual(beforeJson);
    expect(current.state.selection.toJSON()).toEqual(beforeSelection);
  });

  it("creates no Yjs structs or updates when projecting an edit", () => {
    const collaborativeDocument = new Y.Doc();
    host = document.createElement("div");
    document.body.append(host);
    editor = new Editor({
      element: host,
      extensions: buildEditorExtensions(collaborativeDocument),
    });
    editor.commands.setContent("<p>Canonical passage.</p>");
    const stateBefore = Y.encodeStateVector(collaborativeDocument);
    const snapshotBefore = Y.encodeStateAsUpdate(collaborativeDocument);
    let updateCount = 0;
    const onUpdate = (): void => {
      updateCount += 1;
    };
    collaborativeDocument.on("update", onUpdate);

    projectCoworkLedgerDecorations(editor, {
      edits: [
        {
          proposalId: "view-only-1",
          quoteAnchor: {
            exact: "Canonical passage",
            prefix: "",
            suffix: ".",
          },
          replacement: "Proposed passage",
          changeType: "modification",
        },
      ],
      flags: [],
      expressions: [],
      claims: [],
      provenance: [],
    });

    expect(updateCount).toBe(0);
    expect(Y.encodeStateVector(collaborativeDocument)).toEqual(stateBefore);
    expect(Y.encodeStateAsUpdate(collaborativeDocument)).toEqual(snapshotBefore);
    expect(editor.view.dom).toHaveTextContent(
      "Canonical passageProposed passage.",
    );
    collaborativeDocument.off("update", onUpdate);
    editor.destroy();
    editor = null;
    collaborativeDocument.destroy();
  });

  it("renders edit proposals as view-only strike and replacement decorations", () => {
    const current = mountEditor(
      "<p>Keep this. Remove this. Change this.</p>",
    );
    const beforeJson = current.getJSON();
    const beforeHtml = current.getHTML();

    projectCoworkLedgerDecorations(current, {
      edits: [
        {
          proposalId: "insert-1",
          quoteAnchor: {
            exact: "Keep this",
            prefix: "",
            suffix: ". Remove",
          },
          replacement: "Please Keep this now",
          changeType: "insertion",
        },
        {
          proposalId: "delete-1",
          quoteAnchor: {
            exact: "Remove this",
            prefix: "this. ",
            suffix: ". Change",
          },
          replacement: "",
          changeType: "deletion",
        },
        {
          proposalId: "modify-1",
          quoteAnchor: {
            exact: "Change this",
            prefix: "this. ",
            suffix: ".",
          },
          replacement: "Revise this",
          changeType: "modification",
        },
      ],
      flags: [],
      expressions: [],
      claims: [],
      provenance: [],
    });

    expect(
      current.view.dom.querySelector(
        '[data-wb-anchor-id="delete-1"]',
      ),
    ).toHaveClass("wb-cowork-suggestion--deletion");
    expect(
      current.view.dom.querySelector(
        '[data-wb-anchor-id="modify-1"][data-wb-decoration="edit-proposal-replacement"]',
      ),
    ).toHaveTextContent("Revise this");
    expect(
      [...current.view.dom.querySelectorAll<HTMLElement>(
        '[data-wb-anchor-id="insert-1"][data-wb-decoration="edit-proposal-replacement"]',
      )].map((element) => element.textContent),
    ).toEqual(["Please ", " now"]);

    focusCoworkLedgerAnchor(current, {
      id: "modify-1",
      kind: "proposal",
    });
    expect(
      current.view.dom.querySelectorAll(
        '[data-wb-anchor-id="modify-1"].wb-cowork-anchor--active',
      ),
    ).toHaveLength(2);

    expect(current.getJSON()).toEqual(beforeJson);
    expect(current.getHTML()).toBe(beforeHtml);
    expect(current.getText()).toBe("Keep this. Remove this. Change this.");
  });

  it("projects R2 identities into display-only, namespace-qualified decorations", () => {
    const current = mountEditor();
    const beforeJson = current.getJSON();
    const beforeHtml = current.getHTML();

    expect(
      projectCoworkLedgerDecorations(current, {
        edits: [],
        flags: [
          {
            proposalId: "same-id",
            quoteAnchor: {
              exact: "flagged phrase",
              prefix: "A ",
              suffix: ". An",
            },
          },
        ],
        expressions: [
          {
            expressionId: "expression-1",
            spanId: "span-expression",
            quote: "expression phrase",
            claimRef: "claim:same-id",
            claimStatus: "confirmed",
          },
        ],
        claims: [
          {
            claimId: "same-id",
            expressionId: "expression-1",
            spanId: "span-expression",
            quote: "expression phrase",
          },
        ],
        provenance: [
          {
            spanId: "span-confirmed",
            quote: "confirmed phrase",
            trustState: "ai_confirmed",
            producer: "session-1",
            approvalGestureId: "gesture-1",
          },
          {
            spanId: "span-human",
            quote: "human phrase",
            trustState: "human",
            producer: null,
            approvalGestureId: null,
          },
        ],
      }),
    ).toBe(true);

    const flag = current.view.dom.querySelector<HTMLElement>(
      '[data-wb-decoration="flag"]',
    );
    expect(flag).toHaveAttribute("data-wb-anchor-kind", "proposal");
    expect(flag).toHaveAttribute("data-wb-anchor-id", "same-id");
    expect(flag).toHaveClass("wb-cowork-flag-mark");

    expect(
      current.view.dom.querySelector('[data-wb-decoration="expression"]'),
    ).toBeNull();
    setCoworkEditorLens(current, "truth");
    expect(
      current.view.dom.querySelector('[data-wb-decoration="flag"]'),
    ).toBeNull();

    const expression = current.view.dom.querySelector<HTMLElement>(
      '[data-wb-decoration="expression"]',
    );
    expect(expression).toHaveAttribute("data-wb-anchor-kind", "expression");
    expect(expression).toHaveAttribute("data-wb-expression-id", "expression-1");

    const claim = current.view.dom.querySelector<HTMLElement>(
      '[data-wb-claim-ids]',
    );
    expect(claim).toHaveAttribute("data-wb-claim-ids", '["same-id"]');
    expect(claim).toHaveClass("wb-cowork-claim-anchor");

    expect(
      current.view.dom.querySelector('[data-wb-decoration="provenance"]'),
    ).toBeNull();
    expect(
      current.view.dom.querySelector('[data-wb-anchor-id="span-human"]'),
    ).toBeNull();

    // ProseMirror decorations are view state: no ledger annotation enters the
    // editable document or its serialized Markdown/HTML projection.
    expect(current.getJSON()).toEqual(beforeJson);
    expect(current.getHTML()).toBe(beforeHtml);
  });

  it("projects independent provenance axes, unrecorded gaps, and overlap conflicts", () => {
    const current = mountEditor("<p>AI passage and uncovered words.</p>");
    const before = current.getJSON();
    const base = {
      quoteAnchor: { exact: "AI passage", prefix: "", suffix: " and" },
      isDocumentDefault: false,
      currentness: "current" as const,
      source: "paste",
      sourceDetail: "Format: plain text",
      contributors: "No contributors recorded",
      reviewers: "No reviewers recorded",
      attester: "user-1",
      basis: "user_attestation",
      historyCount: 1,
      effectiveCount: 1,
      recordState: "recorded" as const,
    };
    projectCoworkLedgerDecorations(current, {
      edits: [], flags: [], expressions: [], claims: [], provenance: [],
      provenanceOverlay: [
        {
          ...base,
          targetId: "target-a",
          recordId: "record-a",
          authorship: "ai",
          reviewStatus: "not_reviewed",
          resolution: "resolved",
          authorshipFingerprint: "ai:alice",
          reviewFingerprint: "not-reviewed",
          sourceFingerprint: "paste:first",
        },
        {
          ...base,
          targetId: "target-b",
          recordId: "record-b",
          authorship: "ai",
          reviewStatus: "not_reviewed",
          resolution: "resolved",
          authorshipFingerprint: "ai:bob",
          reviewFingerprint: "not-reviewed",
          sourceFingerprint: "paste:first",
        },
      ],
    });
    setCoworkEditorLens(current, "provenance");

    const conflict = current.view.dom.querySelector<HTMLElement>(
      '[data-wb-provenance-conflict="true"]',
    );
    expect(conflict).toHaveClass("wb-cowork-provenance--conflict");
    expect(conflict).toHaveAttribute(
      "data-wb-provenance-ids",
      '["target-a","target-b"]',
    );
    const gap = current.view.dom.querySelector<HTMLElement>(
      '[data-wb-provenance-record-state="unrecorded"]',
    );
    expect(gap).toHaveClass("wb-cowork-provenance--unrecorded");
    expect(gap).not.toHaveAttribute("data-wb-anchor-kind");
    expect(current.getJSON()).toEqual(before);
  });

  it("keeps compatible overlap target health explicit and rejects changed selector context", () => {
    const current = mountEditor("<p>New context Shared passage and tail.</p>");
    const base = {
      quoteAnchor: {
        exact: "Shared passage",
        prefix: "New context ",
        suffix: " and tail.",
      },
      isDocumentDefault: false,
      authorship: "ai" as const,
      reviewStatus: "not_reviewed" as const,
      resolution: "resolved" as const,
      source: "generation",
      sourceDetail: "Model: test-model",
      contributors: "No contributors recorded",
      reviewers: "No reviewers recorded",
      attester: "user-1",
      basis: "system_observation",
      historyCount: 1,
      effectiveCount: 1,
      recordState: "recorded" as const,
      authorshipFingerprint: "ai",
      reviewFingerprint: "not_reviewed",
      sourceFingerprint: "generation:test-model",
    };
    projectCoworkLedgerDecorations(current, {
      edits: [],
      flags: [],
      expressions: [],
      claims: [],
      provenance: [],
      provenanceOverlay: [
        {
          ...base,
          targetId: "current-target",
          recordId: "current-record",
          currentness: "current",
        },
        {
          ...base,
          targetId: "reanchored-target",
          recordId: "reanchored-record",
          currentness: "requires_reanchor",
        },
        {
          ...base,
          targetId: "wrong-context-target",
          recordId: "wrong-context-record",
          currentness: "requires_reanchor",
          quoteAnchor: {
            exact: "Shared passage",
            prefix: "Old context ",
            suffix: " and tail.",
          },
        },
      ],
    });
    setCoworkEditorLens(current, "provenance");

    const shared = current.view.dom.querySelector<HTMLElement>(
      '[data-wb-provenance-ids*="current-target"]',
    );
    expect(shared).toHaveAttribute(
      "data-wb-provenance-currentness",
      "multiple target states",
    );
    expect(shared).toHaveAttribute("data-wb-provenance-conflict", "false");
    expect(
      current.view.dom.querySelector(
        '[data-wb-provenance-ids*="wrong-context-target"]',
      ),
    ).toBeNull();
  });

  it("persists kind-qualified Truth focus without leaking Review marks", () => {
    const current = mountEditor();
    projectCoworkLedgerDecorations(current, {
      edits: [],
      flags: [
        {
          proposalId: "same-id",
          quoteAnchor: {
            exact: "flagged phrase",
            prefix: "",
            suffix: "",
          },
        },
      ],
      expressions: [
        {
          expressionId: "expression-1",
          spanId: "span-expression",
          quote: "expression phrase",
          claimRef: "claim:same-id",
          claimStatus: "confirmed",
        },
      ],
      claims: [
        {
          claimId: "same-id",
          expressionId: "expression-1",
          spanId: "span-expression",
          quote: "expression phrase",
        },
      ],
      provenance: [],
    });
    setCoworkEditorLens(current, "truth");

    focusCoworkLedgerAnchor(
      current,
      { id: "same-id", kind: "claim" },
      true,
    );
    expect(
      current.view.dom.querySelector(
        "[data-wb-claim-ids]",
      ),
    ).toHaveClass("wb-cowork-anchor--active", "wb-cowork-anchor--flash");
    expect(
      current.view.dom.querySelector(
        '[data-wb-anchor-kind="proposal"][data-wb-anchor-id="same-id"]',
      ),
    ).toBeNull();
    expect(readCoworkLedgerDecorationState(current)?.focused).toEqual({
      id: "same-id",
      kind: "claim",
    });

    clearCoworkLedgerAnchorFocus(current);
    expect(
      current.view.dom.querySelector(".wb-cowork-anchor--active"),
    ).toBeNull();
    expect(readCoworkLedgerDecorationState(current)?.focused).toBeNull();
  });

  it("uses selector context to annotate exactly one repeated passage", () => {
    const current = mountEditor(
      "<p>First repeated phrase here. Second repeated phrase there.</p>",
    );

    projectCoworkLedgerDecorations(current, {
      edits: [],
      flags: [],
      expressions: [
        {
          expressionId: "expression-repeated",
          spanId: "span-repeated",
          quote: "repeated phrase",
          quoteAnchor: {
            exact: "repeated phrase",
            prefix: " here. Second ",
            suffix: " there.",
          },
          claimRef: "claim-1",
          claimStatus: "confirmed",
        },
      ],
      claims: [],
      provenance: [],
    });
    setCoworkEditorLens(current, "truth");

    const expressions = current.view.dom.querySelectorAll<HTMLElement>(
      '[data-wb-decoration="expression"]',
    );
    expect(expressions).toHaveLength(1);
    expect(expressions[0]?.previousSibling?.textContent).toContain("Second ");
  });

  it("maps existing decorations across a human edit without changing document data", () => {
    const current = mountEditor();
    projectCoworkLedgerDecorations(current, {
      edits: [],
      flags: [
        {
          proposalId: "flag-1",
          quoteAnchor: {
            exact: "flagged phrase",
            prefix: "A ",
            suffix: ". An",
          },
        },
      ],
      expressions: [],
      claims: [],
      provenance: [],
    });

    current.view.dispatch(current.state.tr.insertText("Intro ", 1));

    const flag = current.view.dom.querySelector<HTMLElement>(
      '[data-wb-decoration="flag"]',
    );
    expect(flag).toHaveTextContent("flagged phrase");
    expect(current.getText()).toContain("Intro A flagged phrase.");
  });
});
