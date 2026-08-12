import type { Editor } from "@tiptap/core";

import {
  projectCoworkLedgerDecorations,
  setCoworkEditorLens,
  type CoworkEditorLens,
  type CoworkLedgerDecorationProjection,
} from "../editor/ledgerDecorations";
import type { ReviewRailData } from "../rail/contracts";
import type {
  ProvenanceAttestation,
  ProvenanceData,
  ProvenanceTarget,
} from "../provenance/view/contracts";
import {
  provenanceAuthorshipFingerprint,
  provenancePersonDetail,
  provenanceReviewFingerprint,
  provenanceSourceDetails,
  provenanceSourceFingerprint,
} from "../provenance/view/semantics";
import { claimRefMatchesId } from "../rail/items";

const EMPTY_LEDGER_PROJECTION: CoworkLedgerDecorationProjection = {
  edits: [],
  flags: [],
  expressions: [],
  claims: [],
  provenance: [],
  evaluations: [],
};

const claimIdFromReference = (reference: string): string | null => {
  if (/^[0-9a-f]{32}$/u.test(reference)) return reference;
  const match = reference.match(/\/claim\/([0-9a-f]{32})$/u);
  return match?.[1] ?? null;
};

/**
 * Convert one authoritative R2-derived rail snapshot into the display-only editor
 * projection. The editor layer receives the same pull as the cards, but only the
 * identities and quote anchors it needs to paint ledger annotations.
 */
export const ledgerDecorationProjectionFromReview = (
  data: ReviewRailData,
): CoworkLedgerDecorationProjection => ({
  edits: data.proposals.flatMap((proposal) =>
    proposal.kind === "edit" && proposal.replacement !== null
      ? [
          {
            proposalId: proposal.proposalId,
            quoteAnchor: proposal.quoteAnchor,
            replacement: proposal.replacement,
            changeType: proposal.changeType ?? "modification",
          },
        ]
      : [],
  ),
  flags: data.proposals.flatMap((proposal) =>
    proposal.kind === "flag"
      ? [
          {
            proposalId: proposal.proposalId,
            quoteAnchor: proposal.quoteAnchor,
          },
        ]
      : [],
  ),
  expressions: data.expressions.map((expression) => ({
    expressionId: expression.expressionId,
    spanId: expression.spanId,
    quote: expression.quote,
    ...(expression.quoteAnchor === undefined
      ? {}
      : { quoteAnchor: expression.quoteAnchor }),
    claimRef: expression.claimRef,
    claimStatus: expression.claimStatus,
  })),
  claims: data.expressions.flatMap((expression) => {
    const reviewClaim = data.claims.find((candidate) =>
      claimRefMatchesId(expression.claimRef, candidate.claimId),
    );
    const claimId =
      reviewClaim?.claimId ?? claimIdFromReference(expression.claimRef);
    return claimId === null
      ? []
      : [
          {
            claimId,
            expressionId: expression.expressionId,
            spanId: expression.spanId,
            quote: expression.quote,
          },
        ];
  }),
  provenance: data.provenanceSpans.map((span) => ({
    spanId: span.spanId,
    quote: span.quote,
    ...(span.quoteAnchor === undefined
      ? {}
      : { quoteAnchor: span.quoteAnchor }),
    trustState: span.trustState,
    producer:
      span.producer === null
        ? null
        : span.producer.sessionId || span.producer.model,
    approvalGestureId: span.approvalGestureId,
  })),
  evaluations: data.evaluationResults.flatMap((result) =>
    result.quoteAnchor === null
      ? []
      : [
          {
            resultId: result.resultId,
            quoteAnchor: result.quoteAnchor,
            resultKind: result.kind,
          },
        ],
  ),
});

const provenanceTargetId = (target: ProvenanceTarget): string =>
  target.projectionId;

const provenanceOverlayRecord = (
  target: ProvenanceTarget,
  record: ProvenanceAttestation,
  isDocumentDefault: boolean,
) => {
  const personList = (
    people: ProvenanceAttestation["authorship"]["contributors"],
    empty: string,
  ): string =>
    people.map(provenancePersonDetail).join(", ") || empty;
  const sourceDetail = provenanceSourceDetails(record).map(
    (detail) => `${detail.label}: ${detail.value}`,
  );
  return ({
  targetId:
    provenanceTargetId(target),
  recordId: record.attestationId,
  quoteAnchor: target.span,
  isDocumentDefault,
  authorship: record.authorship.kind,
  reviewStatus: record.humanReview.status,
  currentness: target.target.currentness,
  resolution: target.resolution,
  source:
    typeof record.source.kind === "string" ? record.source.kind : "unknown",
  sourceDetail:
    sourceDetail.length === 0
      ? "No additional source detail"
      : [...new Set(sourceDetail)].join(" · "),
  contributors: personList(record.authorship.contributors, "No contributors recorded"),
  reviewers: personList(record.humanReview.reviewers, "No reviewers recorded"),
  attester: record.assertedBy.ref ?? record.assertedBy.kind,
  basis: record.basis.kind,
  historyCount: target.history.length,
  effectiveCount: target.effectiveAttestations.length,
  recordState: "recorded" as const,
  authorshipFingerprint: provenanceAuthorshipFingerprint(record),
  reviewFingerprint: provenanceReviewFingerprint(record),
  sourceFingerprint: provenanceSourceFingerprint(record),
  });
};

export const ledgerProvenanceProjection = (data: ProvenanceData) => {
  const project = (target: ProvenanceTarget, isDocumentDefault: boolean) =>
    target.effectiveAttestations.map((record) =>
      provenanceOverlayRecord(target, record, isDocumentDefault),
    );
  return [
    ...(data.documentDefault === null ? [] : project(data.documentDefault, true)),
    ...data.spans.flatMap((target) => project(target, false)),
  ];
};

/**
 * R2 may resolve before or after the editor mounts. This coordinator retains the
 * latest projection and dispatches it whenever both halves are present, without putting
 * React state in the editor plugin.
 */
export class LedgerDecorationProjector {
  #editor: Editor | null = null;
  #projection: CoworkLedgerDecorationProjection | null = null;
  #provenanceOverlay: CoworkLedgerDecorationProjection["provenanceOverlay"] =
    undefined;
  #provenanceDirty = false;
  #lens: CoworkEditorLens = "review";

  attach(editor: Editor): void {
    this.#editor = editor;
    this.#flush();
  }

  detach(): void {
    this.#editor = null;
  }

  setData(data: ReviewRailData): void {
    this.#projection = ledgerDecorationProjectionFromReview(data);
    this.#flush();
  }

  setProvenanceData(data: ProvenanceData | null): void {
    this.#provenanceOverlay =
      data === null ? undefined : ledgerProvenanceProjection(data);
    this.#flush();
  }

  /**
   * A local edit makes the last server head non-current immediately. Whole-document
   * attribution is therefore withheld; exact spans may only reanchor for inspection.
   */
  setProvenanceDirty(dirty: boolean): void {
    if (this.#provenanceDirty === dirty) return;
    this.#provenanceDirty = dirty;
    this.#flush();
  }

  /** Retain the active rail lens across editor remounts and data refreshes. */
  setLens(lens: CoworkEditorLens): void {
    this.#lens = lens;
    if (this.#editor !== null) setCoworkEditorLens(this.#editor, lens);
  }

  clear(): void {
    this.#projection = null;
    this.#provenanceOverlay = undefined;
    this.#provenanceDirty = false;
    if (this.#editor !== null) {
      projectCoworkLedgerDecorations(this.#editor, EMPTY_LEDGER_PROJECTION);
    }
  }

  #flush(): void {
    if (this.#editor === null || this.#projection === null) return;
    const provenanceOverlay = this.#provenanceOverlay;
    const projectedProvenance =
      provenanceOverlay === undefined
        ? undefined
        : this.#provenanceDirty
          ? provenanceOverlay.flatMap((target) =>
              target.isDocumentDefault
                ? []
                : [
                    {
                      ...target,
                      currentness:
                        target.currentness === "current"
                          ? "requires_reanchor" as const
                          : target.currentness,
                    },
                  ],
            )
          : provenanceOverlay;
    projectCoworkLedgerDecorations(this.#editor, {
      ...this.#projection,
      ...(projectedProvenance === undefined
        ? {}
        : { provenanceOverlay: projectedProvenance }),
    });
    setCoworkEditorLens(this.#editor, this.#lens);
  }
}
