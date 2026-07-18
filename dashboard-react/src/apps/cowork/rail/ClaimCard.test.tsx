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
  it("renders the proposition, a non-color status label, and the evidence count", () => {
    renderCard();
    expect(
      screen.getByText(/Latency dropped from 1.8 s to 1.1 s/),
    ).toBeVisible();
    expect(screen.getByText("Confirmed")).toBeVisible();
    expect(screen.getByText("1 evidence span")).toBeVisible();
  });

  it("offers the inspect affordance and passes the span id", async () => {
    const onInspect = vi.fn();
    renderCard({ inspectSpanId: "sp-cl1", onInspect });
    await userEvent.click(
      screen.getByRole("button", { name: "Inspect the sentence" }),
    );
    expect(onInspect).toHaveBeenCalledWith("sp-cl1");
  });

  it("shows a staged claim verb badge", () => {
    renderCard({
      staged: { claimId: "cl1", verb: "challenge", canonicalSha256: "canon" },
    });
    expect(screen.getByText("Staged: Challenge")).toBeVisible();
  });

  it("has no accessibility violations", async () => {
    const { container } = renderCard({
      inspectSpanId: "sp-cl1",
      onInspect: vi.fn(),
      onScrollToAnchor: vi.fn(),
    });
    await expectNoAccessibilityViolations(container);
  });
});
