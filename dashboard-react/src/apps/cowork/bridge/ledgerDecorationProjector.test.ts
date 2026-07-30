import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { afterEach, describe, expect, it } from "vitest";

import { CoworkLedgerDecorations } from "../editor/ledgerDecorations";
import type { ReviewRailData } from "../rail/contracts";
import {
  LedgerDecorationProjector,
  ledgerDecorationProjectionFromReview,
} from "./ledgerDecorationProjector";

const data: ReviewRailData = {
  documentId: "doc-1",
  title: "Document",
  drift: {
    state: "clean",
    openProposalCount: 2,
    openFlagCount: 1,
    lastMaterializedSha256: null,
    currentFileSha256: null,
  },
  verifyCapability: {
    enabled: true,
    contractVersion: 1,
    canRun: true,
    canConfigure: true,
    canCothink: true,
    disabledReason: null,
  },
  verificationConfiguration: {
    schema: "work-buddy.cowork-verify-configuration/v1",
    documentId: "doc-1",
    executionPlan: null,
    coordination: null,
    criteria: [],
  },
  evaluationRuns: [],
  evaluationResults: [],
  verificationRecheckIntents: [],
  cothinkItems: [],
  cothinkOutcomes: [],
  proposals: [
    {
      proposalId: "flag-1",
      kind: "flag",
      quoteAnchor: { exact: "Flagged", prefix: "", suffix: "" },
      replacement: null,
      rationale: "Check this",
      tldr: "Check",
      producer: {
        model: "model-1",
        modelSource: "test",
        sessionId: "session-1",
        surface: "cowork",
      },
      epistemicState: "ai_proposed",
      baseDocSha256: "base",
      canonicalSha256: "canonical-flag",
      baseOk: true,
      status: "open",
      fixesRef: null,
      claimRefs: [],
      createdAt: "2026-07-28T00:00:00Z",
      anchorLabel: "paragraph 1",
      documentOrder: 1,
    },
    {
      proposalId: "edit-1",
      kind: "edit",
      changeType: "modification",
      quoteAnchor: { exact: "Original", prefix: "", suffix: " text" },
      replacement: "Revised",
      rationale: "Clarify this",
      tldr: "Revise",
      producer: {
        model: "model-1",
        modelSource: "test",
        sessionId: "session-1",
        surface: "cowork",
      },
      epistemicState: "ai_proposed",
      baseDocSha256: "base",
      canonicalSha256: "canonical-edit",
      baseOk: true,
      status: "open",
      fixesRef: null,
      claimRefs: [],
      createdAt: "2026-07-28T00:01:00Z",
      anchorLabel: "paragraph 2",
      documentOrder: 2,
    },
  ],
  expressions: [
    {
      expressionId: "expression-1",
      spanId: "span-1",
      nodeIdHint: null,
      quote: "Claim passage",
      claimRef: "claim-1",
      claimStatus: "confirmed",
      claimKind: "fact",
    },
  ],
  provenanceSpans: [
    {
      spanId: "span-1",
      quote: "Claim passage",
      trustState: "ai_confirmed",
      producer: {
        model: "model-1",
        modelSource: "test",
        sessionId: "session-1",
        surface: "cowork",
      },
      approvalGestureId: "gesture-1",
    },
  ],
  claims: [
    {
      claimId: "claim-1",
      proposition: "A claim",
      status: "confirmed",
      claimKind: "fact",
      canonicalSha256: "canonical-claim",
      rationale: "",
      receipts: [],
      anchorLabel: "paragraph 1",
      documentOrder: 2,
    },
  ],
};

let editor: Editor | null = null;

afterEach(() => {
  editor?.destroy();
  editor = null;
});

describe("ledgerDecorationProjectionFromReview", () => {
  it("maps flags, expressions, expression-backed claims, and provenance from one R2 pull", () => {
    expect(ledgerDecorationProjectionFromReview(data)).toEqual({
      edits: [
        {
          proposalId: "edit-1",
          quoteAnchor: { exact: "Original", prefix: "", suffix: " text" },
          replacement: "Revised",
          changeType: "modification",
        },
      ],
      flags: [
        {
          proposalId: "flag-1",
          quoteAnchor: { exact: "Flagged", prefix: "", suffix: "" },
        },
      ],
      expressions: [
        {
          expressionId: "expression-1",
          spanId: "span-1",
          quote: "Claim passage",
          claimRef: "claim-1",
          claimStatus: "confirmed",
        },
      ],
      claims: [
        {
          claimId: "claim-1",
          expressionId: "expression-1",
          spanId: "span-1",
          quote: "Claim passage",
        },
      ],
      evaluations: [],
      provenance: [
        {
          spanId: "span-1",
          quote: "Claim passage",
          trustState: "ai_confirmed",
          producer: "session-1",
          approvalGestureId: "gesture-1",
        },
      ],
    });
  });

  it("retains an R2 pull that arrives before the editor mounts", () => {
    const projector = new LedgerDecorationProjector();
    projector.setData(data);
    editor = new Editor({
      element: document.createElement("div"),
      content: "<p>Flagged. Original text. Claim passage.</p>",
      extensions: [
        StarterKit.configure({ undoRedo: false }),
        CoworkLedgerDecorations,
      ],
    });

    projector.attach(editor);
    expect(
      editor.view.dom.querySelector('[data-wb-decoration="flag"]'),
    ).not.toBeNull();
    expect(
      editor.view.dom.querySelector(
        '[data-wb-decoration="edit-proposal-replacement"]',
      ),
    ).toHaveTextContent("Revised");
    expect(
      editor.view.dom.querySelector('[data-wb-expression-id="expression-1"]'),
    ).toHaveClass(
      "wb-cowork-expression-mark",
      "wb-cowork-claim-anchor",
      "wb-cowork-provenance-tint",
    );

    projector.clear();
    expect(
      editor.view.dom.querySelector(".wb-cowork-ledger-decoration"),
    ).toBeNull();
  });
});
