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
    decisions: {},
    claimDecisions: {},
    inspectSpanByClaim: emptySpanMap,
    grouped: false,
    onSelect: vi.fn(),
    onInspect: vi.fn(),
  };
}

describe("StreamView", () => {
  it("renders every item as a card in document order", () => {
    render(<StreamView {...baseProps()} />);
    const cards = document.querySelectorAll(".wb-cowork-rail__card");
    expect(cards).toHaveLength(5);
    expect(screen.getByText("Add the vault content hash to the cache key.")).toBeVisible();
    expect(screen.getByText("Cite the benchmark file for this figure.")).toBeVisible();
  });

  it("selects a card and reports its kind", async () => {
    const props = baseProps();
    render(<StreamView {...props} />);
    await userEvent.click(
      screen.getByText("Add the vault content hash to the cache key."),
    );
    expect(props.onSelect).toHaveBeenCalledWith("s1", "proposal");
  });

  it("renders the grouped narrow fallback with typed headings", () => {
    render(<StreamView {...baseProps()} grouped />);
    expect(
      screen.getByRole("region", { name: "Suggestions" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Flags" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Claims" })).toBeInTheDocument();
    const suggestions = screen.getByRole("region", { name: "Suggestions" });
    expect(
      suggestions.querySelectorAll(".wb-cowork-rail__card"),
    ).toHaveLength(3);
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
