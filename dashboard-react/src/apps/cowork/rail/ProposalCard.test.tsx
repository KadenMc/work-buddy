import { fireEvent, render, screen } from "@testing-library/react";
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
  it("shows only scan-level information until the card is selected", () => {
    const { rerender } = renderCard();
    expect(screen.getByText("Insertion")).toBeVisible();
    expect(screen.getByText("Add the vault hash to the key.")).toBeVisible();
    expect(screen.queryByText("Include vault state in the key.")).toBeNull();
    expect(screen.queryByText("the set and the vault hash")).toBeNull();
    rerender(
      <ul>
        <ProposalCard proposal={proposal()} selected onSelect={vi.fn()} />
      </ul>,
    );
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
          selected
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

  it("names the concrete target-placement problem without claiming whole-document staleness", () => {
    renderCard({
      proposal: proposal({
        baseOk: false,
        applicability: { status: "target_changed", reason: "target_missing" },
      }),
    });
    expect(screen.getByText("Original passage is no longer present")).toBeVisible();
    expect(screen.queryByText(/older version/iu)).toBeNull();
  });

  it("shows the staged verb badge", () => {
    renderCard({
      staged: { proposalId: "p1", verb: "confirm", canonicalSha256: "c" },
    });
    expect(screen.getByText("Decision: Accept")).toBeVisible();
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

  it("selects from the card chrome without duplicating title-button activation", async () => {
    const onSelect = vi.fn();
    const { container } = renderCard({ onSelect });
    await userEvent.click(screen.getByText("Insertion"));
    expect(onSelect).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByText("Add the vault hash to the key."));
    expect(onSelect).toHaveBeenCalledTimes(2);

    const card = container.querySelector(".wb-cowork-rail__card");
    expect(card).not.toBeNull();
  });

  it("keeps title keyboard activation singular", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    renderCard({ onSelect });
    const title = screen.getByRole("button", { pressed: false });

    title.focus();
    await user.keyboard("{Enter}");
    await user.keyboard(" ");

    expect(onSelect).toHaveBeenCalledTimes(2);
  });

  it("ignores selected detail text but not a selection outside the card", () => {
    const onSelect = vi.fn();
    const outside = document.createElement("p");
    outside.textContent = "Editor selection";
    document.body.append(outside);
    const { container } = renderCard({ selected: true, onSelect });
    const card = container.querySelector<HTMLElement>(".wb-cowork-rail__card");
    const rationale = screen.getByText("Include vault state in the key.");
    expect(card).not.toBeNull();

    const selection = window.getSelection();
    const outsideRange = document.createRange();
    outsideRange.selectNodeContents(outside);
    selection?.removeAllRanges();
    selection?.addRange(outsideRange);
    fireEvent.click(screen.getByText("Insertion"));
    expect(onSelect).toHaveBeenCalledTimes(1);

    const cardRange = document.createRange();
    cardRange.selectNodeContents(rationale);
    selection?.removeAllRanges();
    selection?.addRange(cardRange);
    fireEvent.click(rationale);
    expect(onSelect).toHaveBeenCalledTimes(1);

    selection?.removeAllRanges();
    outside.remove();
  });

  it("exposes a scroll-to-anchor affordance when a handler is wired", async () => {
    const onScrollToAnchor = vi.fn();
    const onSelect = vi.fn();
    renderCard({ onScrollToAnchor, onSelect });
    await userEvent.click(
      screen.getByRole("button", { name: /Go to paragraph 2/ }),
    );
    expect(onScrollToAnchor).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("has no accessibility violations", async () => {
    const { container } = renderCard({ selected: true, onScrollToAnchor: vi.fn() });
    await expectNoAccessibilityViolations(container);
  });
});
