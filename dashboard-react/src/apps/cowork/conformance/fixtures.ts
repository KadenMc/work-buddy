/**
 * Deterministic rail fixtures for the conformance suite. They derive from the
 * shipped demo scene so the proof exercises the same shapes the surface renders,
 * with one addition the demo scene lacks: a proposal whose original passage is
 * no longer present. No production shape is invented here.
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
 * An edit whose immutable original passage no longer resolves. Accept and
 * Amend are unavailable; record-level review decisions remain decidable.
 */
export function staleBaseProposal(): ReviewProposal {
  return {
    ...insertionProposal(),
    proposalId: "stale-1",
    baseOk: false,
    applicability: { status: "target_changed", reason: "target_missing" },
  };
}

/** The full demo review layer, for whole-rail renders. */
export function reviewData(): ReviewRailData {
  return demoReviewData();
}
