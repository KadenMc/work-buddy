import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { ProposalCard } from "./ProposalCard";
import type { ReviewProposal } from "./contracts";

function proposal(overrides: Partial<ReviewProposal> = {}): ReviewProposal {
  return {
    proposalId: "p1",
    kind: "edit",
    changeType: "insertion",
    quoteAnchor: { exact: "the set", prefix: "keys on ", suffix: ", so" },
    replacement: "the set and the vault hash",
    rationale: "Include vault state in the key.",
    tldr: "Add the vault hash to the key.",
    producer: { model: "research-agent", modelSource: "s", sessionId: "sid", surface: "mcp" },
    epistemicState: "ai_proposed",
    baseDocSha256: "b",
    canonicalSha256: "c",
    baseOk: true,
    status: "open",
    fixesRef: null,
    claimRefs: [],
    createdAt: "2026-07-17T00:00:00Z",
    anchorLabel: "paragraph 2",
    documentOrder: 20,
    ...overrides,
  };
}

function renderCard(props: Partial<Parameters<typeof ProposalCard>[0]> = {}) {
  return render(
    <ul>
      <ProposalCard
        proposal={proposal()}
        selected={false}
        onSelect={vi.fn()}
        {...props}
      />
    </ul>,
  );
}

describe("ProposalCard", () => {
  it("renders an insertion card with its kind, tldr, rationale, and inserted text", () => {
    renderCard();
    expect(screen.getByText("Insertion")).toBeVisible();
    expect(screen.getByText("Add the vault hash to the key.")).toBeVisible();
    expect(screen.getByText("Include vault state in the key.")).toBeVisible();
    expect(screen.getByText("the set and the vault hash")).toBeVisible();
    expect(screen.getByText("research-agent")).toBeVisible();
  });

  it("renders a deletion card with struck-through original text", () => {
    render(
      <ul>
        <ProposalCard
          proposal={proposal({
            changeType: "deletion",
            replacement: "",
            quoteAnchor: { exact: "always ", prefix: "We ", suffix: "rebuild" },
          })}
          selected={false}
          onSelect={vi.fn()}
        />
      </ul>,
    );
    expect(screen.getByText("Deletion")).toBeVisible();
    const del = document.querySelector(".wb-cowork-rail__quote-del");
    expect(del?.textContent).toBe("always ");
  });

  it("renders a flag card with no replacement quote", () => {
    renderCard({ proposal: proposal({ kind: "flag", replacement: null }) });
    expect(screen.getByText("Flag")).toBeVisible();
    expect(document.querySelector(".wb-cowork-rail__card-quote")).toBeNull();
  });

  it("shows the stale-base badge with a non-color text label when base_ok is false", () => {
    renderCard({ proposal: proposal({ baseOk: false }) });
    expect(screen.getByText("Stale base, reject or defer only")).toBeVisible();
  });

  it("shows the staged verb badge", () => {
    renderCard({
      staged: { proposalId: "p1", verb: "confirm", canonicalSha256: "c" },
    });
    expect(screen.getByText("Staged: Accept")).toBeVisible();
  });

  it("selects on click and reflects selection with aria-pressed", async () => {
    const onSelect = vi.fn();
    const { rerender } = renderCard({ onSelect });
    const select = screen.getByRole("button", { pressed: false });
    await userEvent.click(select);
    expect(onSelect).toHaveBeenCalledTimes(1);
    rerender(
      <ul>
        <ProposalCard proposal={proposal()} selected onSelect={onSelect} />
      </ul>,
    );
    expect(screen.getByRole("button", { pressed: true })).toBeVisible();
  });

  it("exposes a scroll-to-anchor affordance when a handler is wired", async () => {
    const onScrollToAnchor = vi.fn();
    renderCard({ onScrollToAnchor });
    await userEvent.click(
      screen.getByRole("button", { name: /Go to paragraph 2/ }),
    );
    expect(onScrollToAnchor).toHaveBeenCalledTimes(1);
  });

  it("has no accessibility violations", async () => {
    const { container } = renderCard({ onScrollToAnchor: vi.fn() });
    await expectNoAccessibilityViolations(container);
  });
});
