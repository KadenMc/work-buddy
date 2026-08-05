import type { Editor } from "@tiptap/core";

import {
  projectCoworkLedgerDecorations,
  setCoworkEditorLens,
  type CoworkEditorLens,
  type CoworkLedgerDecorationProjection,
} from "../editor/ledgerDecorations";
import type { ReviewRailData } from "../rail/contracts";
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

/**
 * R2 may resolve before or after the editor mounts. This coordinator retains the
 * latest projection and dispatches it whenever both halves are present, without putting
 * React state in the editor plugin.
 */
export class LedgerDecorationProjector {
  #editor: Editor | null = null;
  #projection: CoworkLedgerDecorationProjection | null = null;
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

  /** Retain the active rail lens across editor remounts and data refreshes. */
  setLens(lens: CoworkEditorLens): void {
    this.#lens = lens;
    if (this.#editor !== null) setCoworkEditorLens(this.#editor, lens);
  }

  clear(): void {
    this.#projection = null;
    if (this.#editor !== null) {
      projectCoworkLedgerDecorations(this.#editor, EMPTY_LEDGER_PROJECTION);
    }
  }

  #flush(): void {
    if (this.#editor === null || this.#projection === null) return;
    projectCoworkLedgerDecorations(this.#editor, this.#projection);
    setCoworkEditorLens(this.#editor, this.#lens);
  }
}
