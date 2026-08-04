import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { demoReviewData } from "./InMemoryReviewProvider";
import { StreamView } from "./StreamView";
import { orderedItems } from "./items";

const items = orderedItems(demoReviewData());
const emptySpanMap = new Map<string, string>();

function baseProps() {
  return {
    items,
    selectedId: null,
    selectedKind: null,
    decisions: {},
    claimDecisions: {},
    inspectSpanByClaim: emptySpanMap,
    onSelect: vi.fn(),
    onInspect: vi.fn(),
  };
}

describe("StreamView", () => {
  it("renders every item as a card in document order", () => {
    render(<StreamView {...baseProps()} />);
    const cards = [...document.querySelectorAll(".wb-cowork-rail__card")];
    expect(cards).toHaveLength(5);
    expect(
      cards.map(
        (card) =>
          card.querySelector(".wb-cowork-rail__card-tldr")?.textContent,
      ),
    ).toEqual(
      items.map((item) =>
        item.kind === "claim" ? item.claim.proposition : item.proposal.tldr,
      ),
    );
  });

  it("selects a card and reports its kind", async () => {
    const props = baseProps();
    render(<StreamView {...props} />);
    await userEvent.click(
      screen.getByText("Add the vault content hash to the cache key."),
    );
    expect(props.onSelect).toHaveBeenCalledWith("s1", "proposal");
  });

  it("navigates explicitly to a card's document passage", async () => {
    const props = {
      ...baseProps(),
      onScrollToAnchor: vi.fn(),
    };
    const first = items[0];
    const anchorLabel =
      first.kind === "claim" ? first.claim.anchorLabel : first.proposal.anchorLabel;
    render(<StreamView {...props} />);

    await userEvent.click(
      screen.getByRole("button", {
        name: `Go to ${anchorLabel} in the document`,
      }),
    );

    expect(props.onScrollToAnchor).toHaveBeenCalledWith(first.id, first.kind);
  });

  it("keeps every card in normal flow across rerenders", () => {
    const props = baseProps();
    const { container, rerender } = render(<StreamView {...props} />);
    rerender(
      <StreamView
        {...props}
        decisions={{
          s1: {
            proposalId: "s1",
            verb: "confirm",
            canonicalSha256: "sha",
          },
        }}
      />,
    );

    for (const card of container.querySelectorAll<HTMLElement>(
      ".wb-cowork-rail__card",
    )) {
      expect(card.style.position).toBe("");
      expect(card.style.transform).toBe("");
    }
    expect(
      container.querySelector(".wb-cowork-rail__card-list"),
    ).not.toHaveAttribute("style");
  });

  it("shows an empty state when there is nothing to review", () => {
    render(<StreamView {...baseProps()} items={[]} />);
    expect(screen.getByText("Nothing to review here.")).toBeVisible();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<StreamView {...baseProps()} />);
    await expectNoAccessibilityViolations(container);
  });
});
