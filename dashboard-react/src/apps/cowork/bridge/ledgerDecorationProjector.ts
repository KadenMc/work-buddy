import type { Editor } from "@tiptap/core";

import {
  projectCoworkLedgerDecorations,
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
  claims: data.claims.flatMap((claim) => {
    const expression = data.expressions.find((candidate) =>
      claimRefMatchesId(candidate.claimRef, claim.claimId),
    );
    return expression === undefined
      ? []
      : [
          {
            claimId: claim.claimId,
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
});

/**
 * R2 may resolve before or after the editor mounts. This coordinator retains the
 * latest projection and dispatches it whenever both halves are present, without putting
 * React state in the editor plugin.
 */
export class LedgerDecorationProjector {
  #editor: Editor | null = null;
  #projection: CoworkLedgerDecorationProjection | null = null;

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

  clear(): void {
    this.#projection = null;
    if (this.#editor !== null) {
      projectCoworkLedgerDecorations(this.#editor, EMPTY_LEDGER_PROJECTION);
    }
  }

  #flush(): void {
    if (this.#editor === null || this.#projection === null) return;
    projectCoworkLedgerDecorations(this.#editor, this.#projection);
  }
}
