import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { ClaimCard } from "./ClaimCard";
import type { ReviewClaim } from "./contracts";

function claim(overrides: Partial<ReviewClaim> = {}): ReviewClaim {
  return {
    claimId: "cl1",
    proposition: "Latency dropped from 1.8 s to 1.1 s after prewarming.",
    status: "confirmed",
    claimKind: "measurement",
    canonicalSha256: "canon",
    rationale: "A measured claim.",
    receipts: [
      {
        evidenceId: "ev1",
        quote: "run A 1.12 s",
        sourceLocator: "bench/a.json",
        trustClass: "measurement",
      },
    ],
    anchorLabel: "paragraph 6",
    documentOrder: 6,
    ...overrides,
  };
}

function renderCard(props: Partial<Parameters<typeof ClaimCard>[0]> = {}) {
  return render(
    <ul>
      <ClaimCard
        claim={claim()}
        selected={false}
        onSelect={vi.fn()}
        {...props}
      />
    </ul>,
  );
}

describe("ClaimCard", () => {
  it("hides claim details until selected", () => {
    renderCard();
    expect(
      screen.getByText(/Latency dropped from 1.8 s to 1.1 s/),
    ).toBeVisible();
    expect(screen.getByText("Confirmed")).toBeVisible();
    expect(screen.queryByText("A measured claim.")).toBeNull();
    expect(screen.queryByText("1 evidence span")).toBeNull();

    renderCard({ selected: true });
    expect(screen.getByText("A measured claim.")).toBeVisible();
    expect(screen.getByText("1 evidence span")).toBeVisible();
  });

  it("offers the inspect affordance and passes the span id", async () => {
    const onInspect = vi.fn();
    renderCard({ selected: true, inspectSpanId: "sp-cl1", onInspect });
    await userEvent.click(
      screen.getByRole("button", { name: "Inspect the sentence" }),
    );
    expect(onInspect).toHaveBeenCalledWith("sp-cl1");
  });

  it("shows a staged claim verb badge", () => {
    renderCard({
      staged: { claimId: "cl1", verb: "challenge", canonicalSha256: "canon" },
    });
    expect(screen.getByText("Decision: Challenge")).toBeVisible();
  });

  it("selects from the card chrome while embedded controls keep their action", async () => {
    const onSelect = vi.fn();
    const onInspect = vi.fn();
    renderCard({ selected: true, onSelect, inspectSpanId: "sp-cl1", onInspect });
    await userEvent.click(screen.getByText("Claim"));
    expect(onSelect).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Inspect the sentence" }));
    expect(onInspect).toHaveBeenCalledWith("sp-cl1");
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("has no accessibility violations", async () => {
    const { container } = renderCard({
      selected: true,
      inspectSpanId: "sp-cl1",
      onInspect: vi.fn(),
      onScrollToAnchor: vi.fn(),
    });
    await expectNoAccessibilityViolations(container);
  });
});
