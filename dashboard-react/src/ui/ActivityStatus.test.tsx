import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAccessibilityViolations } from "../test/setup";
import { ActivityStatus } from "./ActivityStatus";

describe("ActivityStatus", () => {
  it("exposes the label through one status region", () => {
    render(<ActivityStatus label="Checking the folder" />);

    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("Checking the folder");
  });

  it("keeps a ticking detail out of the announced region", () => {
    render(
      <ActivityStatus label="Checking the folder" detail="1,204 items checked" />,
    );

    const region = screen.getByRole("status");
    const detail = screen.getByText("1,204 items checked");

    // `role="status"` carries an implicit polite, atomic live region. A detail that
    // rises through a long operation would re-announce the whole message on every
    // tick, so it renders outside that region and out of the accessibility tree.
    expect(region).not.toContainElement(detail);
    expect(detail).toHaveAttribute("aria-hidden", "true");
  });

  it("renders no second line when the caller gives no detail", () => {
    render(<ActivityStatus label="Checking the folder" />);

    const region = screen.getByRole("status");

    expect(region.parentElement).toHaveTextContent("Checking the folder");
    expect(region.parentElement?.childElementCount).toBe(1);
  });

  it("carries no progressbar semantics", () => {
    const { container } = render(
      <ActivityStatus label="Checking the folder" detail="1,204 items checked" />,
    );

    expect(container.querySelector("[aria-valuenow]")).toBeNull();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByRole("meter")).toBeNull();
  });

  it("passes an accessibility audit with and without a detail", async () => {
    const { container, rerender } = render(
      <ActivityStatus label="Checking the folder" />,
    );
    await expectNoAccessibilityViolations(container);

    rerender(
      <ActivityStatus label="Checking the folder" detail="1,204 items checked" />,
    );
    await expectNoAccessibilityViolations(container);
  });
});
