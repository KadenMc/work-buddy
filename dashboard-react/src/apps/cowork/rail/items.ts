/**
 * Pure derivation of the unified review-item list from ReviewRailData. The
 * stream, the groups, and the queue all walk the same document-ordered list,
 * and the filter lens narrows it. Suggestions are edit proposals, flags are
 * flag proposals.
 */

import type { ReviewProposal, ReviewRailData } from "./contracts";
import type { RailFilter } from "./store";
import type { FilterCounts } from "./FilterLens";

export interface RailItem {
  readonly kind: "proposal";
  readonly id: string;
  readonly documentOrder: number;
  readonly proposal: ReviewProposal;
}

/** The kind of typed group an item belongs to. */
export type RailGroup = "suggestions" | "flags";

/** Stable UI identity for a review proposal. */
export function railItemKey(item: RailItem): string {
  return `${item.kind}:${item.id}`;
}

/** Whether an item is the exact kind-qualified rail selection. */
export function isSelectedItem(
  item: RailItem,
  selectedId: string | null,
  selectedKind: RailItem["kind"] | null,
): boolean {
  return item.id === selectedId && item.kind === selectedKind;
}

export function groupOf(item: RailItem): RailGroup {
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
  for (const item of items) {
    const group = groupOf(item);
    if (group === "suggestions") suggestions += 1;
    else if (group === "flags") flags += 1;
  }
  return { all: items.length, suggestions, flags };
}
