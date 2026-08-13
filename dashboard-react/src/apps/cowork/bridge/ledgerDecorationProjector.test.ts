import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { afterEach, describe, expect, it } from "vitest";

import { CoworkLedgerDecorations } from "../editor/ledgerDecorations";
import type { ReviewRailData } from "../rail/contracts";
import type {
  ProvenanceAttestation,
  ProvenanceData,
  ProvenanceTarget,
} from "../provenance/view/contracts";
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

const provenanceRecord = (
  id: string,
  kind: "document_version" | "document_span",
  spanId: string | null,
): ProvenanceAttestation => ({
  attestationId: id,
  at: "2026-08-12T12:00:00Z",
  assertedBy: { kind: "human", ref: "user-1", meta: null },
  scope: {
    kind,
    documentVersionId: kind === "document_version" ? "version-1" : null,
    documentSpanId: spanId,
    structuredHeadSha256: "a".repeat(64),
  },
  authorship: { kind: "ai", contributors: [] },
  humanReview: { status: "not_reviewed", reviewers: [] },
  source: { kind: "paste" },
  basis: { kind: "user_attestation", ref: null },
  supersedesId: null,
  canonicalSha256: "b".repeat(64),
});

const provenanceTarget = (
  id: string,
  record: ProvenanceAttestation,
  span: ProvenanceTarget["span"],
): ProvenanceTarget => ({
  projectionId: id,
  target: {
    kind: record.scope.kind,
    documentVersionId: record.scope.documentVersionId,
    documentSpanId: record.scope.documentSpanId,
    structuredHeadSha256: record.scope.structuredHeadSha256,
    currentness: "current",
  },
  span,
  effectiveAttestations: [record],
  effectiveAttestation: record,
  resolution: "resolved",
  reviewEligibility: "eligible",
  issue: null,
  history: [record],
});

const provenanceData = (): ProvenanceData => {
  const defaultRecord = provenanceRecord("default-record", "document_version", null);
  const spanRecord = provenanceRecord("span-record", "document_span", "span-1");
  return {
    schema: "cowork-provenance-view/v1",
    currentStructuredHeadSha256: "a".repeat(64),
    documentDefault: provenanceTarget("default-target", defaultRecord, null),
    spans: [
      provenanceTarget("span-target", spanRecord, {
        exact: "AI passage",
        prefix: "",
        suffix: " and",
      }),
    ],
    history: [defaultRecord, spanRecord],
    summary: {
      totalTargets: 2,
      currentSpanCount: 1,
      aiUnreviewedCount: 2,
      reviewedCount: 0,
      conflictedCount: 0,
      staleCount: 0,
      unrecorded: false,
    },
  };
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
    ).toBeNull();

    projector.setLens("truth");
    expect(
      editor.view.dom.querySelector('[data-wb-decoration="flag"]'),
    ).toBeNull();
    expect(
      editor.view.dom.querySelector('[data-wb-expression-id="expression-1"]'),
    ).toHaveClass(
      "wb-cowork-expression-mark",
      "wb-cowork-claim-anchor",
    );

    projector.clear();
    expect(
      editor.view.dom.querySelector(".wb-cowork-ledger-decoration"),
    ).toBeNull();
  });

  it("suppresses whole-document attribution and re-resolves exact spans while locally dirty", () => {
    const projector = new LedgerDecorationProjector();
    projector.setData({ ...data, provenanceSpans: [] });
    projector.setProvenanceData(provenanceData());
    editor = new Editor({
      element: document.createElement("div"),
      content: "<p>AI passage and default words.</p>",
      extensions: [
        StarterKit.configure({ undoRedo: false }),
        CoworkLedgerDecorations,
      ],
    });
    projector.attach(editor);
    projector.setLens("provenance");
    expect(
      editor.view.dom.querySelectorAll(
        '[data-wb-provenance-id="default-target"]',
      ).length,
    ).toBeGreaterThan(0);

    projector.setProvenanceDirty(true);
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-id="default-target"]',
      ),
    ).toBeNull();
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-id="span-target"]',
      ),
    ).toHaveAttribute("data-wb-provenance-currentness", "requires_reanchor");

    editor.commands.setContent("<p>The passage was replaced.</p>");
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-id="span-target"]',
      ),
    ).toBeNull();
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-record-state="unrecorded"]',
      ),
    ).not.toBeNull();

    projector.setProvenanceDirty(false);
    projector.setProvenanceData(provenanceData());
    expect(
      editor.view.dom.querySelector(
        '[data-wb-provenance-id="default-target"]',
      ),
    ).not.toBeNull();
  });
});
