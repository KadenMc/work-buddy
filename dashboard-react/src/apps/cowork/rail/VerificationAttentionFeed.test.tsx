import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  CothinkItem,
  CothinkOutcome,
  EvaluationResult,
  EvaluationRunSummary,
  VerificationRecheckIntent,
} from "./contracts";
import { VerificationAttentionFeed } from "./VerificationAttentionFeed";

const run: EvaluationRunSummary = {
  runId: "run-1",
  status: "completed",
  purpose: "document_review",
  targetLabel: "Whole document",
  coverageLabel: "Complete exact-string scan",
  currentVersion: true,
  resultCount: 1,
  surfacedResultCount: 1,
  coordinationStatus: "completed",
  providerLabel: "Codex",
  providerId: "codex",
  modelLabel: "GPT",
  modelId: "gpt",
  createdAt: "2026-07-28T00:00:00Z",
  finishedAt: "2026-07-28T00:00:01Z",
};

const result: EvaluationResult = {
  resultId: "result-1",
  runId: run.runId,
  kind: "nonconforming",
  criterionLabel: "Approved terminology",
  criterionStatement: "Use document target.",
  checkLabel: "Terminology exact match",
  methodLabel: "Deterministic exact-string scan",
  explanation: "The deprecated term appears once.",
  quoteAnchor: { exact: "Co-work scope", prefix: "", suffix: "" },
  coverageLabel: "Complete exact-string scan",
  limitations: ["Does not detect synonyms."],
  currentVersion: true,
  disposition: "surface_proposal",
  canonicalSha256: "a".repeat(64),
  proposalIds: ["proposal-1"],
  createdAt: "2026-07-28T00:00:01Z",
};

const cothink: CothinkItem = {
  itemId: "cothink-1",
  subtype: "alternative_perspective",
  content: "What changes if the audience treats this as a reversible choice?",
  rationale: "The document assumes the decision is permanent.",
  targetLabel: "Decision",
  quoteAnchor: null,
  status: "open",
  currentVersion: true,
  canonicalSha256: "b".repeat(64),
  createdAt: "2026-07-28T00:00:02Z",
};

const noItemOutcome: CothinkOutcome = {
  outcomeId: "cothink-outcome-1",
  status: "completed_no_useful_item",
  rationale: "No useful alternative was found without manufacturing one.",
  targetLabel: "Whole document",
  currentVersion: true,
  providerId: "codex",
  modelId: "gpt",
  createdAt: "2026-07-28T00:00:03Z",
  finishedAt: "2026-07-28T00:00:04Z",
};

const recheckIntent: VerificationRecheckIntent = {
  intentId: "recheck-1",
  sittingId: "sitting-1",
  sourceRunId: "run-1",
  proposalIds: ["proposal-1"],
  pendingProposalIds: ["proposal-1"],
  fulfilledByRunIds: [],
  committedAt: "2026-07-28T00:00:05Z",
  userGoal: "Recheck the applied terminology correction.",
  protectedIntent: "Preserve substantive meaning.",
  status: "pending_capture",
  originalActionTarget: {
    actionSnapshotId: "action-1",
    source: "whole_document",
    label: "Whole document",
    kind: "document",
    selector: { kind: "document" },
    targetTextSha256: "c".repeat(64),
    targetReference: null,
    targetReferenceSha256: null,
  },
  execution: {
    providerId: "codex",
    modelId: "gpt",
    providerLabel: "Codex",
    modelLabel: "GPT",
  },
  requires: {
    freshActionSnapshot: true,
    freshModelCallAuthorization: true,
    sameTargetSource: true,
    sameTargetReference: true,
    exactTargetResolution: true,
    userAffirmedExactTargetRequired: false,
    allowWidenToWholeDocument: false,
  },
};

describe("VerificationAttentionFeed", () => {
  it("keeps evidence, proposals, and Co-think actions semantically distinct", async () => {
    const reveal = vi.fn();
    const openProposal = vi.fn();
    const discuss = vi.fn();
    const act = vi.fn();
    render(
      <VerificationAttentionFeed
        results={[result]}
        cothinkItems={[cothink]}
        cothinkOutcomes={[]}
        onRevealResult={reveal}
        onOpenProposal={openProposal}
        onDiscussCothink={discuss}
        onCothinkAction={act}
      />,
    );

    expect(screen.getByText("Requirement not met")).toBeVisible();
    expect(screen.getAllByText("Complete exact-string scan")).toHaveLength(1);
    expect(screen.getByText("Co-think · Alternative perspective")).toBeVisible();
    expect(screen.queryByText(/severity/i)).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Show evidence" }));
    await userEvent.click(
      screen.getByRole("button", { name: "Review correction" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Discuss" }));
    await userEvent.click(
      screen.getByRole("button", { name: "Keep for later" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(reveal).toHaveBeenCalledWith(result);
    expect(openProposal).toHaveBeenCalledWith("proposal-1");
    expect(discuss).toHaveBeenCalledWith(cothink);
    expect(act).toHaveBeenCalledWith(cothink, "park");
    expect(act).toHaveBeenCalledWith(cothink, "dismiss");
  });

  it("keeps Dismiss available after an item is kept for later", async () => {
    const act = vi.fn();
    render(
      <VerificationAttentionFeed
        results={[]}
        cothinkItems={[{ ...cothink, status: "parked" }]}
        cothinkOutcomes={[]}
        onCothinkAction={act}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Kept for later" }),
    ).toBeDisabled();
    const dismiss = screen.getByRole("button", { name: "Dismiss" });
    expect(dismiss).toBeEnabled();
    await userEvent.click(dismiss);
    expect(act).toHaveBeenCalledWith(
      expect.objectContaining({
        itemId: cothink.itemId,
        status: "parked",
      }),
      "dismiss",
    );
  });

  it("shows durable no-item and unavailable Co-think outcomes", () => {
    render(
      <VerificationAttentionFeed
        results={[]}
        cothinkItems={[]}
        cothinkOutcomes={[
          noItemOutcome,
          {
            ...noItemOutcome,
            outcomeId: "cothink-outcome-2",
            status: "unavailable",
            rationale: "The selected account-backed agent is unavailable.",
          },
        ]}
      />,
    );

    expect(screen.getByText("No useful alternative found")).toBeVisible();
    expect(screen.getByText("Perspective unavailable")).toBeVisible();
    expect(
      screen.getByText(
        "No useful alternative was found without manufacturing one.",
      ),
    ).toBeVisible();
  });

  it("requires a present user action before a durable correction recheck", async () => {
    const recheck = vi.fn();
    render(
      <VerificationAttentionFeed
        results={[]}
        recheckIntents={[recheckIntent]}
        cothinkItems={[]}
        cothinkOutcomes={[]}
        onRecheckIntent={recheck}
      />,
    );

    expect(screen.getByText("Correction ready to recheck")).toBeVisible();
    expect(recheck).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Recheck in Verify" }),
    );
    expect(recheck).toHaveBeenCalledWith(recheckIntent);
  });

  it("routes a legacy target through an explicit target-setting action", async () => {
    const recheck = vi.fn();
    const legacyIntent: VerificationRecheckIntent = {
      ...recheckIntent,
      intentId: "recheck-legacy",
      status: "user_action_required",
      originalActionTarget: {
        ...recheckIntent.originalActionTarget,
        source: null,
        label: "Earlier methods passage",
        kind: "text_quote",
        selector: {
          kind: "text_quote",
          exact: "Earlier methods passage",
          prefix: "",
          suffix: "",
          start: 0,
          end: 23,
        },
      },
    };
    render(
      <VerificationAttentionFeed
        results={[]}
        recheckIntents={[legacyIntent]}
        cothinkItems={[]}
        cothinkOutcomes={[]}
        onRecheckIntent={recheck}
      />,
    );

    expect(
      screen.getByText("Choose a target for the follow-up check"),
    ).toBeVisible();
    await userEvent.click(
      screen.getByRole("button", { name: "Set target and recheck" }),
    );
    expect(recheck).toHaveBeenCalledWith(legacyIntent);
  });

});
