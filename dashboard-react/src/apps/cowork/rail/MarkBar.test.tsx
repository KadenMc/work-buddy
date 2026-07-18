import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { MarkBar, type MarkBarProps } from "./MarkBar";
import type { ReviewClaim, ReviewProposal } from "./contracts";

function proposal(overrides: Partial<ReviewProposal> = {}): ReviewProposal {
  return {
    proposalId: "p1",
    kind: "edit",
    changeType: "insertion",
    quoteAnchor: { exact: "x", prefix: "", suffix: "" },
    replacement: "new text",
    rationale: "r",
    tldr: "Add the vault hash.",
    producer: { model: "m", modelSource: "s", sessionId: "sid", surface: "mcp" },
    epistemicState: "ai_proposed",
    baseDocSha256: "b",
    canonicalSha256: "canon-p1",
    baseOk: true,
    status: "open",
    fixesRef: null,
    claimRefs: [],
    createdAt: "2026-07-17T00:00:00Z",
    anchorLabel: "paragraph 1",
    documentOrder: 1,
    ...overrides,
  };
}

function claim(overrides: Partial<ReviewClaim> = {}): ReviewClaim {
  return {
    claimId: "cl1",
    proposition: "Latency dropped after prewarming.",
    status: "confirmed",
    claimKind: "measurement",
    canonicalSha256: "canon-cl1",
    rationale: "Measured.",
    receipts: [],
    anchorLabel: "paragraph 6",
    documentOrder: 6,
    ...overrides,
  };
}

function handlers(): Pick<
  MarkBarProps,
  "onStageProposal" | "onStageClaim" | "onClearProposal" | "onClearClaim"
> {
  return {
    onStageProposal: vi.fn(),
    onStageClaim: vi.fn(),
    onClearProposal: vi.fn(),
    onClearClaim: vi.fn(),
  };
}

describe("MarkBar edit verbs", () => {
  it("renders the seven edit verbs and stages a no-input verb immediately", async () => {
    const cbs = handlers();
    render(
      <MarkBar target={{ kind: "proposal", proposal: proposal() }} {...cbs} />,
    );
    for (const label of [
      "Accept",
      "Amend",
      "Reject",
      "Reject as false",
      "Reject as preference",
      "Redirect",
      "Defer",
    ]) {
      expect(screen.getByRole("button", { name: label })).toBeVisible();
    }
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(cbs.onStageProposal).toHaveBeenCalledWith({
      proposalId: "p1",
      verb: "confirm",
      canonicalSha256: "canon-p1",
    });
  });

  it("collects an amended replacement before staging edit_confirm", async () => {
    const cbs = handlers();
    render(
      <MarkBar target={{ kind: "proposal", proposal: proposal() }} {...cbs} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Amend" }));
    const field = screen.getByLabelText("Your replacement");
    expect(field).toHaveValue("new text");
    await userEvent.clear(field);
    await userEvent.type(field, "my version");
    await userEvent.click(screen.getByRole("button", { name: "Stage" }));
    expect(cbs.onStageProposal).toHaveBeenCalledWith({
      proposalId: "p1",
      verb: "edit_confirm",
      canonicalSha256: "canon-p1",
      amendContent: "my version",
    });
  });

  it("requires a note before staging a redirect", async () => {
    const cbs = handlers();
    render(
      <MarkBar target={{ kind: "proposal", proposal: proposal() }} {...cbs} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Redirect" }));
    await userEvent.type(
      screen.getByLabelText("Guidance for the agent"),
      "narrow the claim",
    );
    await userEvent.click(screen.getByRole("button", { name: "Stage" }));
    expect(cbs.onStageProposal).toHaveBeenCalledWith({
      proposalId: "p1",
      verb: "redirect",
      canonicalSha256: "canon-p1",
      redirectNote: "narrow the claim",
    });
  });

  it("collects a verbatim negation for reject_as_false when there are no claim refs", async () => {
    const cbs = handlers();
    render(
      <MarkBar target={{ kind: "proposal", proposal: proposal() }} {...cbs} />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Reject as false" }),
    );
    await userEvent.type(
      screen.getByLabelText(/recorded as a negation/),
      "The latency did not change.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Stage" }));
    expect(cbs.onStageProposal).toHaveBeenCalledWith({
      proposalId: "p1",
      verb: "reject_as_false",
      canonicalSha256: "canon-p1",
      negationText: "The latency did not change.",
    });
  });

  it("stages reject_as_false immediately when a claim ref is present", async () => {
    const cbs = handlers();
    render(
      <MarkBar
        target={{
          kind: "proposal",
          proposal: proposal({
            claimRefs: [{ claim: "wb-truth://c1", role: "instantiation" }],
          }),
        }}
        {...cbs}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Reject as false" }),
    );
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(cbs.onStageProposal).toHaveBeenCalledWith({
      proposalId: "p1",
      verb: "reject_as_false",
      canonicalSha256: "canon-p1",
    });
  });

  it("disables all but reject and defer on a stale base and states the reason", () => {
    const cbs = handlers();
    render(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal({ baseOk: false }) }}
        {...cbs}
      />,
    );
    expect(screen.getByText(/Stale base/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Accept" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Amend" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Redirect" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Defer" })).toBeEnabled();
  });

  it("toggles a staged no-input verb off on a second click", async () => {
    const cbs = handlers();
    render(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal() }}
        stagedProposal={{
          proposalId: "p1",
          verb: "defer",
          canonicalSha256: "canon-p1",
        }}
        {...cbs}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Defer" }));
    expect(cbs.onClearProposal).toHaveBeenCalledWith("p1");
  });
});

describe("MarkBar flag verbs", () => {
  it("renders Endorse, Dismiss, and Redirect and stages endorse", async () => {
    const cbs = handlers();
    render(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal({ kind: "flag", replacement: null }) }}
        {...cbs}
      />,
    );
    expect(screen.getByRole("button", { name: "Endorse" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Endorse" }));
    expect(cbs.onStageProposal).toHaveBeenCalledWith({
      proposalId: "p1",
      verb: "endorse",
      canonicalSha256: "canon-p1",
    });
  });
});

describe("MarkBar claim verbs", () => {
  it("renders the six committed claim verbs and stages a claim confirm", async () => {
    const cbs = handlers();
    render(<MarkBar target={{ kind: "claim", claim: claim() }} {...cbs} />);
    for (const label of [
      "Confirm",
      "Reject",
      "Challenge",
      "Supersede",
      "Redact",
      "Propose",
    ]) {
      expect(screen.getByRole("button", { name: label })).toBeVisible();
    }
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(cbs.onStageClaim).toHaveBeenCalledWith({
      claimId: "cl1",
      verb: "confirm",
      canonicalSha256: "canon-cl1",
    });
  });

  it("has no accessibility violations", async () => {
    const cbs = handlers();
    const { container } = render(
      <MarkBar target={{ kind: "proposal", proposal: proposal() }} {...cbs} />,
    );
    await expectNoAccessibilityViolations(container);
  });
});
