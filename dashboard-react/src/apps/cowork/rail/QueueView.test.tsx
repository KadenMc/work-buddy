import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { demoReviewData } from "./InMemoryReviewProvider";
import { QueueView } from "./QueueView";
import { orderedItems } from "./items";

const items = orderedItems(demoReviewData());

function baseProps() {
  return {
    items,
    index: 0,
    decisions: {},
    claimDecisions: {},
    inspectSpanByClaim: new Map<string, string>(),
    onNavigate: vi.fn(),
    onActivate: vi.fn(),
    onInspect: vi.fn(),
  };
}

describe("QueueView", () => {
  it("shows the progress indicator for the focused item", () => {
    render(<QueueView {...baseProps()} />);
    expect(screen.getByText("Item 1")).toBeVisible();
    expect(screen.getByText(/of 5/)).toBeVisible();
    expect(screen.getByText("5 undecided")).toBeVisible();
  });

  it("navigates with the inverted j and k binding", async () => {
    const props = baseProps();
    render(<QueueView {...props} index={1} />);
    await userEvent.keyboard("k");
    expect(props.onNavigate).toHaveBeenLastCalledWith(1);
    await userEvent.keyboard("j");
    expect(props.onNavigate).toHaveBeenLastCalledWith(-1);
  });

  it("honours a configured binding", async () => {
    const props = baseProps();
    render(<QueueView {...props} bindings={{ prev: "p", next: "n" }} />);
    await userEvent.keyboard("n");
    expect(props.onNavigate).toHaveBeenLastCalledWith(1);
  });

  it("does not handle queue shortcuts while Review is hidden", async () => {
    const props = baseProps();
    render(
      <QueueView
        {...props}
        index={1}
        keyboardNavigationEnabled={false}
      />,
    );
    await userEvent.keyboard("kj");
    expect(props.onNavigate).not.toHaveBeenCalled();
  });

  it("lists every item and marks decided ones", () => {
    const props = baseProps();
    render(
      <QueueView
        {...props}
        index={0}
        decisions={{
          s3: { proposalId: "s3", verb: "reject_plain", canonicalSha256: "c" },
        }}
      />,
    );
    // The focused item (index 0) reads "now", the decided s3 reads "decided".
    expect(screen.getByText("now")).toBeVisible();
    expect(screen.getByText("decided")).toBeVisible();
  });

  it("names modification proposals accurately in the all-items list", () => {
    const first = items[0];
    if (first === undefined || first.kind === "claim") {
      throw new Error("The first demo item must be a proposal.");
    }
    const modificationItems = [
      {
        ...first,
        proposal: {
          ...first.proposal,
          changeType: "modification" as const,
          tldr: "Clarify the cache key.",
        },
      },
      ...items.slice(1),
    ];
    render(
      <QueueView
        {...baseProps()}
        items={modificationItems}
      />,
    );
    expect(
      screen.getByText("Modification, Clarify the cache key."),
    ).toBeVisible();
  });

  it("jumps to a clicked all-items row by a relative delta", async () => {
    const props = baseProps();
    render(<QueueView {...props} index={0} />);
    const rows = document.querySelectorAll<HTMLElement>(
      ".wb-cowork-rail__allitems-row",
    );
    // The third all-items row is two steps below the current focus.
    await userEvent.click(rows[2]);
    expect(props.onNavigate).toHaveBeenCalledWith(2);
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<QueueView {...baseProps()} />);
    await expectNoAccessibilityViolations(container);
  });
});
