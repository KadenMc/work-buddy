import { describe, expect, it } from "vitest";

import { demoReviewData } from "./InMemoryReviewProvider";
import {
  filterCounts,
  groupOf,
  matchesFilter,
  orderedItems,
  visibleItems,
} from "./items";

describe("review item derivation", () => {
  it("orders proposals and claims by document order", () => {
    const items = orderedItems(demoReviewData());
    const orders = items.map((item) => item.documentOrder);
    const sorted = [...orders].sort((a, b) => a - b);
    expect(orders).toEqual(sorted);
  });

  it("counts suggestions, flags, and claims to match the SP-6 scene", () => {
    // Two insertions plus one deletion are suggestions, one flag, one claim.
    expect(filterCounts(demoReviewData())).toEqual({
      all: 5,
      suggestions: 3,
      flags: 1,
      claims: 1,
    });
  });

  it("classifies each item into its typed group", () => {
    const items = orderedItems(demoReviewData());
    const flag = items.find(
      (item) => item.kind === "proposal" && item.proposal.kind === "flag",
    );
    const claim = items.find((item) => item.kind === "claim");
    const edit = items.find(
      (item) => item.kind === "proposal" && item.proposal.kind === "edit",
    );
    expect(flag && groupOf(flag)).toBe("flags");
    expect(claim && groupOf(claim)).toBe("claims");
    expect(edit && groupOf(edit)).toBe("suggestions");
  });

  it("filters to a single group with the lens", () => {
    const data = demoReviewData();
    expect(visibleItems(data, "all")).toHaveLength(5);
    expect(visibleItems(data, "suggestions")).toHaveLength(3);
    expect(visibleItems(data, "flags")).toHaveLength(1);
    expect(visibleItems(data, "claims")).toHaveLength(1);
  });

  it("matchesFilter passes everything under all", () => {
    const item = orderedItems(demoReviewData())[0];
    expect(matchesFilter(item, "all")).toBe(true);
  });
});
