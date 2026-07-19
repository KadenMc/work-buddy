import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { demoReviewData } from "./InMemoryReviewProvider";
import { Inspector } from "./Inspector";

describe("Inspector (read-only)", () => {
  it("reveals the expressions, provenance, and open items under a passage", () => {
    render(
      <Inspector spanId="sp-cl1" data={demoReviewData()} onClose={vi.fn()} />,
    );
    expect(screen.getByText("Under this sentence")).toBeVisible();
    // The claim underneath, by its wb-truth reference and its status.
    expect(screen.getByText("wb-truth://demo/claim/cl1")).toBeVisible();
    expect(screen.getByText("Confirmed")).toBeVisible();
    // The flag anchored on the same sentence appears as an open item.
    expect(screen.getByText(/Cite the benchmark file/)).toBeVisible();
  });

  it("shows the provenance of a confirmed AI span", () => {
    render(
      <Inspector spanId="sp-c1" data={demoReviewData()} onClose={vi.fn()} />,
    );
    expect(
      screen.getByText("AI-written, human-confirmed"),
    ).toBeVisible();
  });

  it("is read-only, with no verb buttons", () => {
    render(
      <Inspector spanId="sp-cl1" data={demoReviewData()} onClose={vi.fn()} />,
    );
    expect(
      screen.queryByRole("button", { name: "Confirm" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Accept" }),
    ).not.toBeInTheDocument();
  });

  it("closes on request", async () => {
    const onClose = vi.fn();
    render(
      <Inspector spanId="sp-cl1" data={demoReviewData()} onClose={onClose} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <Inspector spanId="sp-cl1" data={demoReviewData()} onClose={vi.fn()} />,
    );
    await expectNoAccessibilityViolations(container);
  });
});
