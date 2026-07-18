import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useIsNarrow } from "./useIsNarrow";

function Harness({ width }: { width: number }) {
  const [narrow, ref] = useIsNarrow(360);
  return (
    <div
      data-testid="box"
      data-narrow={String(narrow)}
      ref={(element) => {
        if (element !== null) {
          element.getBoundingClientRect = () =>
            ({ width }) as DOMRect;
        }
        ref(element);
      }}
    />
  );
}

/** A ResizeObserver stub that measures once on observe. */
class ImmediateResizeObserver {
  constructor(private readonly callback: () => void) {}
  observe(): void {
    this.callback();
  }
  unobserve(): void {}
  disconnect(): void {}
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useIsNarrow", () => {
  it("reports not-narrow without a ResizeObserver", () => {
    vi.stubGlobal("ResizeObserver", undefined);
    const { getByTestId } = render(<Harness width={200} />);
    expect(getByTestId("box")).toHaveAttribute("data-narrow", "false");
  });

  it("reports narrow when the measured width is below the threshold", () => {
    vi.stubGlobal("ResizeObserver", ImmediateResizeObserver);
    const { getByTestId } = render(<Harness width={300} />);
    expect(getByTestId("box")).toHaveAttribute("data-narrow", "true");
  });

  it("reports not-narrow when the measured width is above the threshold", () => {
    vi.stubGlobal("ResizeObserver", ImmediateResizeObserver);
    const { getByTestId } = render(<Harness width={800} />);
    expect(getByTestId("box")).toHaveAttribute("data-narrow", "false");
  });
});
