import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AnchorRectSource } from "./provider";
import { useAlignedStream } from "./useAlignedStream";

function Harness({ source }: { source?: AnchorRectSource }) {
  const controller = useAlignedStream({
    anchorRects: source,
    anchors: [
      { id: "a", kind: "proposal" },
      { id: "b", kind: "claim" },
    ],
    gap: 8,
  });
  return (
    <div data-testid="root" data-aligned={String(controller.aligned)}>
      <ul ref={controller.aligned ? controller.registerContainer : undefined}>
        <li
          data-testid="card-a"
          ref={controller.registerCard("a", "proposal")}
        />
        <li data-testid="card-b" ref={controller.registerCard("b", "claim")} />
      </ul>
    </div>
  );
}

describe("useAlignedStream", () => {
  it("stays in the degrade path with no anchor-rect source", () => {
    const { getByTestId } = render(<Harness />);
    expect(getByTestId("root")).toHaveAttribute("data-aligned", "false");
    // No positioning is written when alignment is inactive.
    expect(getByTestId("card-a").style.transform).toBe("");
  });

  it("positions cards at their anchors and resolves a clustered overlap", async () => {
    const source: AnchorRectSource = {
      // Two anchors 5px apart, so the second card must be pushed below the first.
      anchorRect: (id) =>
        id === "a" ? { top: 100, height: 0 } : { top: 105, height: 0 },
      scrollToAnchor: vi.fn(),
      focusAnchor: vi.fn(),
      clearFocusedAnchor: vi.fn(),
      subscribe: () => () => {},
    };
    const { getByTestId } = render(<Harness source={source} />);
    expect(getByTestId("root")).toHaveAttribute("data-aligned", "true");

    await waitFor(() => {
      expect(getByTestId("card-a").style.transform).toBe("translateY(100px)");
    });
    // b would overlap a at 105, so it cascades to 100 + 0 height + 8 gap.
    expect(getByTestId("card-b").style.transform).toBe("translateY(108px)");
  });

  it("degrades every card and clears stale placement when one anchor is lost", async () => {
    let secondResolved = true;
    let notify: (() => void) | undefined;
    const source: AnchorRectSource = {
      anchorRect: (id) => {
        if (id === "b" && !secondResolved) return null;
        return { top: id === "a" ? 100 : 180, height: 0 };
      },
      scrollToAnchor: vi.fn(),
      focusAnchor: vi.fn(),
      clearFocusedAnchor: vi.fn(),
      subscribe: (listener) => {
        notify = listener;
        return () => {
          notify = undefined;
        };
      },
    };
    const { getByTestId } = render(<Harness source={source} />);

    await waitFor(() => {
      expect(getByTestId("card-a").style.transform).toBe("translateY(100px)");
      expect(getByTestId("card-b").style.transform).toBe("translateY(180px)");
    });

    secondResolved = false;
    notify?.();

    await waitFor(() => {
      expect(getByTestId("card-a").style.transform).toBe("");
      expect(getByTestId("card-b").style.transform).toBe("");
    });
  });
});
