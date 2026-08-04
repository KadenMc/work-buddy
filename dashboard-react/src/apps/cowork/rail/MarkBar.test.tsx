import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { DEFAULT_COWORK_SHORTCUT_BINDINGS } from "../keyboard";
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
  it("dispatches the advertised Queue decision keys through the button actions", async () => {
    const cbs = handlers();
    render(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal() }}
        showHotkeys
        keyboardShortcutsEnabled
        {...cbs}
      />,
    );

    await userEvent.keyboard("a");
    expect(cbs.onStageProposal).toHaveBeenLastCalledWith({
      proposalId: "p1",
      verb: "confirm",
      canonicalSha256: "canon-p1",
    });

    await userEvent.keyboard("e");
    expect(screen.getByLabelText("Your replacement")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await userEvent.keyboard("x");
    expect(cbs.onStageProposal).toHaveBeenLastCalledWith({
      proposalId: "p1",
      verb: "reject_plain",
      canonicalSha256: "canon-p1",
    });

    await userEvent.keyboard(".");
    expect(cbs.onStageProposal).toHaveBeenLastCalledWith({
      proposalId: "p1",
      verb: "defer",
      canonicalSha256: "canon-p1",
    });
  });

  it("derives visible hints, aria shortcuts, and dispatch from custom bindings", async () => {
    const cbs = handlers();
    render(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal() }}
        bindings={{
          ...DEFAULT_COWORK_SHORTCUT_BINDINGS,
          accept: "Mod+Enter",
        }}
        showHotkeys
        keyboardShortcutsEnabled
        {...cbs}
      />,
    );
    const accept = screen.getByRole("button", { name: "Accept" });
    expect(accept).toHaveAttribute(
      "aria-keyshortcuts",
      "Control+Enter Meta+Enter",
    );
    expect(accept).toHaveTextContent("Ctrl/⌘ + Enter");
    await userEvent.keyboard("a");
    expect(cbs.onStageProposal).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: "Enter", ctrlKey: true });
    expect(cbs.onStageProposal).toHaveBeenCalledTimes(1);
  });

  it("ignores editable input, unrelated modifiers, and inactive Review", async () => {
    const cbs = handlers();
    const { rerender } = render(
      <>
        <input aria-label="Draft" />
        <MarkBar
          target={{ kind: "proposal", proposal: proposal() }}
          keyboardShortcutsEnabled
          {...cbs}
        />
      </>,
    );
    await userEvent.click(screen.getByRole("textbox", { name: "Draft" }));
    await userEvent.keyboard("a");
    expect(cbs.onStageProposal).not.toHaveBeenCalled();
    await userEvent.click(document.body);
    await userEvent.keyboard("{Control>}a{/Control}");
    expect(cbs.onStageProposal).not.toHaveBeenCalled();

    rerender(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal() }}
        keyboardShortcutsEnabled={false}
        {...cbs}
      />,
    );
    await userEvent.keyboard("a");
    expect(cbs.onStageProposal).not.toHaveBeenCalled();
  });

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

  it("focuses an opened amendment and discards it when the target changes", async () => {
    const cbs = handlers();
    const { rerender } = render(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal() }}
        keyboardShortcutsEnabled
        {...cbs}
      />,
    );

    await userEvent.keyboard("e");
    const firstDraft = screen.getByLabelText("Your replacement");
    expect(firstDraft).toHaveFocus();
    await userEvent.clear(firstDraft);
    await userEvent.type(firstDraft, "Only for proposal one");

    rerender(
      <MarkBar
        target={{
          kind: "proposal",
          proposal: proposal({
            proposalId: "p2",
            canonicalSha256: "canon-p2",
            replacement: "proposal two text",
          }),
        }}
        keyboardShortcutsEnabled
        {...cbs}
      />,
    );

    expect(screen.queryByLabelText("Your replacement")).not.toBeInTheDocument();
    expect(cbs.onStageProposal).not.toHaveBeenCalled();
    await userEvent.keyboard("e");
    expect(screen.getByLabelText("Your replacement")).toHaveValue(
      "proposal two text",
    );
  });

  it("ignores repeated, composing, and control-activation key events", () => {
    const cbs = handlers();
    render(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal() }}
        bindings={{ ...DEFAULT_COWORK_SHORTCUT_BINDINGS, accept: "Enter" }}
        keyboardShortcutsEnabled
        {...cbs}
      />,
    );

    fireEvent.keyDown(window, { key: "Enter", repeat: true });
    fireEvent.keyDown(window, { key: "Enter", isComposing: true });
    const reject = screen.getByRole("button", { name: "Reject" });
    reject.focus();
    fireEvent.keyDown(reject, { key: "Enter" });

    expect(cbs.onStageProposal).not.toHaveBeenCalled();
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

  it("collects a verbatim preferred phrasing for reject_as_preference", async () => {
    const cbs = handlers();
    render(
      <MarkBar target={{ kind: "proposal", proposal: proposal() }} {...cbs} />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Reject as preference" }),
    );
    await userEvent.type(
      screen.getByLabelText(/recorded as a preference/),
      "Keep the original wording.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Stage" }));
    expect(cbs.onStageProposal).toHaveBeenCalledWith({
      proposalId: "p1",
      verb: "reject_as_preference",
      canonicalSha256: "canon-p1",
      preferenceText: "Keep the original wording.",
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

  it("disables only text mutation when the original passage cannot be placed", () => {
    const cbs = handlers();
    render(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal({ baseOk: false }) }}
        {...cbs}
      />,
    );
    expect(screen.getByText(/original passage cannot be placed safely/i)).toBeVisible();
    expect(screen.getByRole("button", { name: "Accept" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Amend" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Redirect" })).toBeEnabled();
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

  it("disables every staging control while Review is submitting", async () => {
    const cbs = handlers();
    const { rerender } = render(
      <MarkBar target={{ kind: "proposal", proposal: proposal() }} {...cbs} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Amend" }));

    rerender(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal() }}
        disabled
        {...cbs}
      />,
    );

    for (const label of [
      "Accept",
      "Amend",
      "Reject",
      "Reject as false",
      "Reject as preference",
      "Redirect",
      "Defer",
      "Stage",
      "Cancel",
    ]) {
      expect(screen.getByRole("button", { name: label })).toBeDisabled();
    }
    expect(screen.getByLabelText("Your replacement")).toBeDisabled();
    expect(cbs.onStageProposal).not.toHaveBeenCalled();
    expect(cbs.onClearProposal).not.toHaveBeenCalled();
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

  it("maps the positive and negative Queue shortcuts to flag verbs", async () => {
    const cbs = handlers();
    render(
      <MarkBar
        target={{ kind: "proposal", proposal: proposal({ kind: "flag", replacement: null }) }}
        keyboardShortcutsEnabled
        {...cbs}
      />,
    );
    await userEvent.keyboard("a");
    expect(cbs.onStageProposal).toHaveBeenLastCalledWith(
      expect.objectContaining({ verb: "endorse" }),
    );
    await userEvent.keyboard("x");
    expect(cbs.onStageProposal).toHaveBeenLastCalledWith(
      expect.objectContaining({ verb: "dismiss" }),
    );
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

  it("maps the positive and negative Queue shortcuts to claim verbs", async () => {
    const cbs = handlers();
    render(
      <MarkBar
        target={{ kind: "claim", claim: claim() }}
        keyboardShortcutsEnabled
        {...cbs}
      />,
    );
    await userEvent.keyboard("a");
    expect(cbs.onStageClaim).toHaveBeenLastCalledWith(
      expect.objectContaining({ verb: "confirm" }),
    );
    await userEvent.keyboard("x");
    expect(cbs.onStageClaim).toHaveBeenLastCalledWith(
      expect.objectContaining({ verb: "reject" }),
    );
  });

  it("has no accessibility violations", async () => {
    const cbs = handlers();
    const { container } = render(
      <MarkBar target={{ kind: "proposal", proposal: proposal() }} {...cbs} />,
    );
    await expectNoAccessibilityViolations(container);
  });
});
