/**
 * One claim-review card (PRD job 2, the six claim verbs live on the mark bar).
 * It shows the claim proposition, its lifecycle status with a non-color label,
 * its evidence receipts, and an inspect affordance into the read-only sentence
 * inspector. Delivered through the review provider seam, the shape carries only
 * what the card and the six verbs need.
 */

import type {
  ClaimStatus,
  ReviewClaim,
  StagedClaimDecision,
} from "./contracts";
import { CLAIM_VERB_LABEL } from "./verbs";

export interface ClaimCardProps {
  readonly claim: ReviewClaim;
  readonly selected: boolean;
  readonly staged?: StagedClaimDecision;
  onSelect(): void;
  /** The span to open in the inspector, when this claim has an expression. */
  readonly inspectSpanId?: string;
  onInspect?(spanId: string): void;
  onScrollToAnchor?(): void;
  cardRef?: (element: HTMLElement | null) => void;
}

const STATUS_LABEL: Record<ClaimStatus, string> = {
  proposed: "Proposed",
  confirmed: "Confirmed",
  challenged: "Challenged",
  rejected: "Rejected",
  superseded: "Superseded",
  retracted: "Retracted",
  expired: "Expired",
};

export function ClaimCard({
  claim,
  selected,
  staged,
  onSelect,
  inspectSpanId,
  onInspect,
  onScrollToAnchor,
  cardRef,
}: ClaimCardProps) {
  return (
    <li
      ref={cardRef}
      className="wb-cowork-rail__card"
      data-kind="claim"
      data-selected={selected ? "true" : undefined}
      data-staged={staged !== undefined ? "true" : undefined}
    >
      <div className="wb-cowork-rail__card-head">
        <span className="wb-cowork-rail__card-kind" data-kind="claim">
          Claim
        </span>
        <span
          className="wb-cowork-rail__claim-status"
          data-status={claim.status}
        >
          {STATUS_LABEL[claim.status]}
        </span>
        {onScrollToAnchor !== undefined ? (
          <button
            type="button"
            className="wb-cowork-rail__card-jump"
            onClick={onScrollToAnchor}
            aria-label={`Go to ${claim.anchorLabel} in the document`}
          >
            {claim.anchorLabel}
          </button>
        ) : (
          <span className="wb-cowork-rail__card-anchor">
            {claim.anchorLabel}
          </span>
        )}
      </div>

      <button
        type="button"
        className="wb-cowork-rail__card-select"
        aria-pressed={selected}
        onClick={onSelect}
      >
        <span className="wb-cowork-rail__card-tldr">{claim.proposition}</span>
      </button>

      <p className="wb-cowork-rail__card-rationale">{claim.rationale}</p>

      <p className="wb-cowork-rail__claim-evidence">
        {claim.receipts.length} evidence{" "}
        {claim.receipts.length === 1 ? "span" : "spans"}
      </p>

      {inspectSpanId !== undefined && onInspect !== undefined ? (
        <button
          type="button"
          className="wb-cowork-rail__inspect-link"
          onClick={() => onInspect(inspectSpanId)}
        >
          Inspect the sentence
        </button>
      ) : null}

      {staged !== undefined ? (
        <p className="wb-cowork-rail__card-badge is-staged" role="status">
          Staged: {CLAIM_VERB_LABEL[staged.verb]}
        </p>
      ) : null}
    </li>
  );
}
