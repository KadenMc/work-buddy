/**
 * Dashboard-citizenship proof (PRD I18) for every member of the CoworkRail
 * family in isolation, so a violation is pinned to its component rather than
 * hidden in the composed surface. Covered: the proposal cards (insertion,
 * deletion, flag), the claim card, the filter lens, the mark bar for each verb
 * group including the reject-as-preference inline input, the passage inspector,
 * the drift strip, and the narrow grouped stream fallback.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import {
  ClaimCard,
  FilterLens,
  Inspector,
  MarkBar,
  ProposalCard,
  RailDriftStrip,
  StreamView,
  orderedItems,
  type MarkBarTarget,
} from "../rail";
import {
  deletionProposal,
  demoClaim,
  flagProposal,
  insertionProposal,
  reviewData,
  staleBaseProposal,
} from "./fixtures";

const noop = () => undefined;

function inList(node: ReactNode) {
  return <ul>{node}</ul>;
}

describe("CoworkRail family accessibility", () => {
  it("clears axe on an insertion proposal card", async () => {
    const { container } = render(
      inList(
        <ProposalCard
          proposal={insertionProposal()}
          selected={false}
          onSelect={noop}
        />,
      ),
    );
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on a deletion proposal card with a scroll-to affordance", async () => {
    const { container } = render(
      inList(
        <ProposalCard
          proposal={deletionProposal()}
          selected
          onSelect={noop}
          onScrollToAnchor={noop}
        />,
      ),
    );
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on a flag card", async () => {
    const { container } = render(
      inList(
        <ProposalCard proposal={flagProposal()} selected={false} onSelect={noop} />,
      ),
    );
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on a stale-base proposal card", async () => {
    const { container } = render(
      inList(
        <ProposalCard
          proposal={staleBaseProposal()}
          selected={false}
          onSelect={noop}
        />,
      ),
    );
    // The stale state is announced in text, not by colour alone.
    expect(screen.getByText(/Stale base/)).toBeVisible();
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on a claim card with an inspect affordance", async () => {
    const { container } = render(
      inList(
        <ClaimCard
          claim={demoClaim()}
          selected={false}
          onSelect={noop}
          inspectSpanId="sp-cl1"
          onInspect={noop}
        />,
      ),
    );
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on the filter lens", async () => {
    const { container } = render(
      <FilterLens
        filter="all"
        counts={{ all: 5, suggestions: 3, flags: 1, claims: 1 }}
        onChange={noop}
      />,
    );
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on the drift strip", async () => {
    const { container } = render(
      <RailDriftStrip title={reviewData().title} drift={reviewData().drift} />,
    );
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on the mark bar for an edit proposal", async () => {
    const target: MarkBarTarget = {
      kind: "proposal",
      proposal: insertionProposal(),
    };
    const { container } = render(
      <MarkBar
        target={target}
        onStageProposal={noop}
        onStageClaim={noop}
        onClearProposal={noop}
        onClearClaim={noop}
      />,
    );
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on the mark bar for a flag", async () => {
    const target: MarkBarTarget = { kind: "proposal", proposal: flagProposal() };
    const { container } = render(
      <MarkBar
        target={target}
        onStageProposal={noop}
        onStageClaim={noop}
        onClearProposal={noop}
        onClearClaim={noop}
      />,
    );
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on the mark bar for a claim", async () => {
    const target: MarkBarTarget = { kind: "claim", claim: demoClaim() };
    const { container } = render(
      <MarkBar
        target={target}
        onStageProposal={noop}
        onStageClaim={noop}
        onClearProposal={noop}
        onClearClaim={noop}
      />,
    );
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe with the reject-as-preference inline input open", async () => {
    const target: MarkBarTarget = {
      kind: "proposal",
      proposal: insertionProposal(),
    };
    const { container } = render(
      <MarkBar
        target={target}
        onStageProposal={noop}
        onStageClaim={noop}
        onClearProposal={noop}
        onClearClaim={noop}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Reject as preference" }),
    );
    // The inline field carries a bound label, so the input is named for axe.
    expect(
      screen.getByLabelText("Your preferred phrasing, recorded as a preference"),
    ).toBeVisible();
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on the passage inspector", async () => {
    const { container } = render(
      <Inspector spanId="sp-c1" data={reviewData()} onClose={noop} />,
    );
    await expectNoAccessibilityViolations(container);
  });

  it("clears axe on the narrow grouped stream fallback", async () => {
    const items = orderedItems(reviewData());
    const { container } = render(
      <StreamView
        items={items}
        selectedId={null}
        decisions={{}}
        claimDecisions={{}}
        inspectSpanByClaim={new Map()}
        grouped
        onSelect={noop}
        onInspect={noop}
      />,
    );
    // The grouped fallback labels each type section in text.
    expect(screen.getByRole("region", { name: "Suggestions" })).toBeVisible();
    await expectNoAccessibilityViolations(container);
  });
});
