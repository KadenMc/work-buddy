/**
 * Deterministic rail fixtures for the conformance suite. They derive from the
 * shipped demo scene so the proof exercises the same shapes the surface renders,
 * with one addition the demo scene lacks: a stale-base proposal, whose mark bar
 * disables every accept-family verb (S6). No production shape is invented here.
 */

import {
  demoReviewData,
  type ReviewClaim,
  type ReviewProposal,
  type ReviewRailData,
} from "../rail";

/** The four demo proposals: two insertions, one deletion, one flag. */
export function demoProposals(): readonly ReviewProposal[] {
  return demoReviewData().proposals;
}

/** The one demo claim (a confirmed measurement with two evidence receipts). */
export function demoClaim(): ReviewClaim {
  const claim = demoReviewData().claims[0];
  if (claim === undefined) {
    throw new Error("The demo scene must carry at least one claim.");
  }
  return claim;
}

function proposalOfKind(
  kind: "insertion" | "deletion" | "flag",
): ReviewProposal {
  const match = demoProposals().find((proposal) =>
    kind === "flag"
      ? proposal.kind === "flag"
      : proposal.kind === "edit" && proposal.changeType === kind,
  );
  if (match === undefined) {
    throw new Error(`The demo scene must carry a ${kind} proposal.`);
  }
  return match;
}

export const insertionProposal = (): ReviewProposal => proposalOfKind("insertion");
export const deletionProposal = (): ReviewProposal => proposalOfKind("deletion");
export const flagProposal = (): ReviewProposal => proposalOfKind("flag");

/**
 * A stale-base edit proposal. The document moved on since it was drafted, so
 * `baseOk` is false and only the reject family and Defer stay decidable.
 */
export function staleBaseProposal(): ReviewProposal {
  return { ...insertionProposal(), proposalId: "stale-1", baseOk: false };
}

/** The full demo review layer, for whole-rail renders. */
export function reviewData(): ReviewRailData {
  return demoReviewData();
}
