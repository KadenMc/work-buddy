/**
 * Pure derivation of the unified review-item list from ReviewRailData. The
 * stream, the groups, and the queue all walk the same document-ordered list,
 * and the filter lens narrows it. Suggestions are edit proposals, flags are
 * flag proposals, claims are the claim-review cards (SP-6 counts).
 */

import type { ReviewClaim, ReviewProposal, ReviewRailData } from "./contracts";
import type { RailFilter } from "./store";
import type { FilterCounts } from "./FilterLens";

export type RailItem =
  | {
      readonly kind: "proposal";
      readonly id: string;
      readonly documentOrder: number;
      readonly proposal: ReviewProposal;
    }
  | {
      readonly kind: "claim";
      readonly id: string;
      readonly documentOrder: number;
      readonly claim: ReviewClaim;
    };

/** The kind of typed group an item belongs to. */
export type RailGroup = "suggestions" | "flags" | "claims";

export function groupOf(item: RailItem): RailGroup {
  if (item.kind === "claim") return "claims";
  return item.proposal.kind === "flag" ? "flags" : "suggestions";
}

/** Every review item in document order. */
export function orderedItems(data: ReviewRailData): RailItem[] {
  const items: RailItem[] = [
    ...data.proposals.map(
      (proposal): RailItem => ({
        kind: "proposal",
        id: proposal.proposalId,
        documentOrder: proposal.documentOrder,
        proposal,
      }),
    ),
    ...data.claims.map(
      (claim): RailItem => ({
        kind: "claim",
        id: claim.claimId,
        documentOrder: claim.documentOrder,
        claim,
      }),
    ),
  ];
  return items.sort((a, b) => a.documentOrder - b.documentOrder);
}

export function matchesFilter(item: RailItem, filter: RailFilter): boolean {
  if (filter === "all") return true;
  return groupOf(item) === filter;
}

/** The document-ordered items visible under the active filter. */
export function visibleItems(
  data: ReviewRailData,
  filter: RailFilter,
): RailItem[] {
  return orderedItems(data).filter((item) => matchesFilter(item, filter));
}

/** Per-group counts for the filter lens chips. */
export function filterCounts(data: ReviewRailData): FilterCounts {
  const items = orderedItems(data);
  let suggestions = 0;
  let flags = 0;
  let claims = 0;
  for (const item of items) {
    const group = groupOf(item);
    if (group === "suggestions") suggestions += 1;
    else if (group === "flags") flags += 1;
    else claims += 1;
  }
  return { all: items.length, suggestions, flags, claims };
}
