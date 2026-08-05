import type { MouseEvent } from "react";

import type {
  TruthClaimSummary,
  TruthPassageConnection,
} from "./contracts";
import { isTruthFact } from "./contracts";

const INTERACTIVE =
  "button, a, input, textarea, select, summary, [role='button'], [role='link']";

const activateContainer = (
  event: MouseEvent<HTMLElement>,
  onSelect: () => void,
): void => {
  const target = event.target as Element | null;
  if (target !== null && target.closest(INTERACTIVE) !== null) return;
  const selection = event.currentTarget.ownerDocument.getSelection?.();
  if (
    selection !== undefined &&
    selection !== null &&
    !selection.isCollapsed &&
    selection.toString().trim().length > 0
  ) {
    return;
  }
  onSelect();
};

const statusLabel = (claim: TruthClaimSummary): string => {
  const labels: Record<TruthClaimSummary["baseStatus"], string> = {
    proposed: "Proposed",
    confirmed: "Confirmed",
    rejected: "Rejected",
    expired: "Expired",
    challenged: "Challenged",
    superseded: "Superseded",
    retracted: "Retracted",
    unknown: "Unknown",
  };
  const base = isTruthFact(claim) ? "Fact" : labels[claim.baseStatus];
  const overlays: string[] = [];
  if (claim.redacted || claim.health === "redacted") overlays.push("Redacted");
  if (claim.voided || claim.health === "voided") overlays.push("Voided");
  if (claim.health === "conflict") overlays.push("Conflict");
  if (claim.health === "failed") overlays.push("Failed");
  if (claim.needsReview || claim.health === "needs_review") {
    overlays.push("Needs review");
  }
  return overlays.length === 0 ? base : `${base} · ${overlays.join(" · ")}`;
};

const cardState = (claim: TruthClaimSummary): string => {
  if (claim.redacted || claim.health === "redacted") return "redacted";
  if (claim.voided || claim.health === "voided") return "voided";
  if (claim.health === "failed") return "failed";
  if (claim.health === "conflict") return "conflict";
  if (claim.needsReview || claim.health === "needs_review") return "needs_review";
  return claim.baseStatus;
};

const claimKindLabel = (kind: string): string => {
  if (kind === "fact") return "Factual claim";
  return kind.split("_").join(" ");
};

export interface TruthClaimCardProps {
  readonly claim: TruthClaimSummary;
  onSelect(): void;
  onRevealPassage?(connection: TruthPassageConnection): void;
}

export function TruthClaimCard({
  claim,
  onSelect,
  onRevealPassage,
}: TruthClaimCardProps) {
  const currentConnections = claim.connections.filter(
    (connection) => connection.currentDocument,
  );
  return (
    <li
      className="wb-cowork-truth__claim-card"
      data-status={cardState(claim)}
      onClick={(event) => activateContainer(event, onSelect)}
    >
      <div className="wb-cowork-truth__claim-head">
        <span className="wb-cowork-truth__claim-kind">
          {claimKindLabel(claim.claimKind)}
        </span>
        <span
          className="wb-cowork-truth__claim-status"
          data-status={cardState(claim)}
        >
          {statusLabel(claim)}
        </span>
      </div>
      <button
        type="button"
        className="wb-cowork-truth__claim-select"
        data-truth-claim-id={claim.claimId}
        onClick={onSelect}
      >
        {claim.proposition}
      </button>
      <p className="wb-cowork-truth__claim-meta">
        <span>
          {claim.evidenceCount} evidence {claim.evidenceCount === 1 ? "receipt" : "receipts"}
        </span>
        <span aria-hidden="true">·</span>
        <span>
          {claim.connectionCount} document {claim.connectionCount === 1 ? "connection" : "connections"}
        </span>
      </p>
      {currentConnections.length === 1 && onRevealPassage !== undefined ? (
        <button
          type="button"
          className="wb-cowork-truth__passage-button"
          onClick={() => onRevealPassage(currentConnections[0])}
        >
          Show in document
        </button>
      ) : currentConnections.length > 1 ? (
        <span className="wb-cowork-truth__unconnected">
          {currentConnections.length} passages in this document
        </span>
      ) : claim.connectionCount === 0 ? (
        <span className="wb-cowork-truth__unconnected">Not connected to prose</span>
      ) : null}
    </li>
  );
}
