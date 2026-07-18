import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AnchorRectSource } from "./provider";
import { useAlignedStream } from "./useAlignedStream";

function Harness({ source }: { source?: AnchorRectSource }) {
  const controller = useAlignedStream({
    anchorRects: source,
    ids: ["a", "b"],
    gap: 8,
  });
  return (
    <div data-testid="root" data-aligned={String(controller.aligned)}>
      <ul ref={controller.aligned ? controller.registerContainer : undefined}>
        <li data-testid="card-a" ref={controller.registerCard("a")} />
        <li data-testid="card-b" ref={controller.registerCard("b")} />
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
});
