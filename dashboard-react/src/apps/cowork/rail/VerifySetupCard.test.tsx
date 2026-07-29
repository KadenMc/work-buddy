import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  CoworkVerifyCapability,
  VerificationConfiguration,
} from "./contracts";
import { VerifySetupCard } from "./VerifySetupCard";

const capability: CoworkVerifyCapability = {
  enabled: true,
  contractVersion: 1,
  canRun: true,
  canConfigure: true,
  canCothink: true,
  disabledReason: null,
};

const configuration: VerificationConfiguration = {
  schema: "work-buddy.cowork-verify-configuration/v1",
  documentId: "doc-1",
  executionPlan: null,
  coordination: {
    required: true,
    selection: "explicit_provider_and_model_at_run_start",
    contentBoundary: "entire_frozen_document",
    egressClass: "account_backed_agent",
    externalEgress: true,
    costCeilingUsdPerWorker: 2,
    separateReviserForFindings: true,
    pattern: "coordinator_then_optional_reviser_then_coordinator",
    baseWorkerCalls: 1,
    maximumWorkerCalls: 3,
  },
  criteria: [
    {
      id: "criterion-1",
      stableKey: "terminology_exact_match",
      version: 1,
      title: "Preferred terminology",
      description: "Use the configured preferred established term.",
      kind: "terminology",
      definitionOrigin: "system",
      author: { kind: "system", ref: "cowork-verify", meta: null },
      activationId: "activation-1",
      enabled: true,
      required: false,
      locked: false,
      activationOrigin: "system",
      authorizedBy: { kind: "system", ref: "cowork-verify", meta: null },
      operationalState: "active",
      availableCheckCount: 1,
      totalCheckCount: 1,
      issues: [],
      checks: [
        {
          id: "check-1",
          stableKey: "terminology_exact_match",
          version: 1,
          title: "Terminology exact-match check",
          mechanism: "deterministic",
          executorRef: "local:exact-match",
          limitations: ["Exact, case-sensitive matching only."],
          definitionOrigin: "system",
          author: { kind: "system", ref: "cowork-verify", meta: null },
          dataSharingClass: "local_only",
          externalEgress: false,
          dataSharingBasis: "admitted_deterministic_executor",
          availability: "available",
          unavailableReason: null,
          executionLocation: "local",
          bindingId: "binding-1",
          selected: true,
          configuration: {},
        },
      ],
    },
  ],
};

describe("VerifySetupCard", () => {
  it("explains criterion, method, provenance, limits, and future-run semantics", async () => {
    const user = userEvent.setup();
    render(
      <VerifySetupCard
        capability={capability}
        configuration={configuration}
        onSetEnabled={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    await user.click(screen.getByText("Verify setup"));
    expect(screen.getByText(/Changes apply to the next run/u)).toBeVisible();
    expect(screen.queryByText("Model coordination")).not.toBeInTheDocument();
    expect(screen.getByText("Built in · Runs next time · Optional")).toBeVisible();
    await user.click(screen.getByText(/Terminology exact-match check/u));
    expect(
      screen.getByText(
        "In-process deterministic checker · checker egress: none",
      ),
    ).toBeVisible();
    expect(
      screen.getByText("Exact, case-sensitive matching only."),
    ).toBeVisible();
  });

  it("submits an exact activation precondition when toggled", async () => {
    const user = userEvent.setup();
    const onSetEnabled = vi.fn().mockResolvedValue(undefined);
    render(
      <VerifySetupCard
        capability={capability}
        configuration={configuration}
        onSetEnabled={onSetEnabled}
      />,
    );

    await user.click(screen.getByText("Verify setup"));
    const toggle = screen.getByRole("checkbox", {
      name: "Preferred terminology: include in Verify runs",
    });
    await user.click(toggle);
    expect(toggle).toHaveAccessibleName(
      "Preferred terminology: include in Verify runs",
    );
    expect(onSetEnabled).toHaveBeenCalledWith(
      "terminology_exact_match",
      false,
      "activation-1",
    );
  });

  it("reports a criterion activation as busy until its authoritative mutation settles", async () => {
    const user = userEvent.setup();
    let settleMutation!: () => void;
    const onSetEnabled = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settleMutation = resolve;
        }),
    );
    const onBusyChange = vi.fn();
    render(
      <VerifySetupCard
        capability={capability}
        configuration={configuration}
        onSetEnabled={onSetEnabled}
        onBusyChange={onBusyChange}
      />,
    );

    await user.click(screen.getByText("Verify setup"));
    await user.click(
      screen.getByRole("checkbox", {
        name: "Preferred terminology: include in Verify runs",
      }),
    );
    await waitFor(() => {
      expect(onBusyChange).toHaveBeenLastCalledWith(true);
    });

    await act(async () => {
      settleMutation();
    });
    await waitFor(() => {
      expect(onBusyChange).toHaveBeenLastCalledWith(false);
    });
  });

  it("reports a criterion draft as busy until its save settles", async () => {
    const user = userEvent.setup();
    let settleDraft!: () => void;
    const onCreateDraft = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settleDraft = resolve;
        }),
    );
    const onBusyChange = vi.fn();
    render(
      <VerifySetupCard
        capability={capability}
        configuration={configuration}
        onCreateDraft={onCreateDraft}
        onBusyChange={onBusyChange}
      />,
    );

    await user.click(screen.getByText("Verify setup"));
    await user.click(screen.getByText("Add a user-authored criterion"));
    await user.type(
      screen.getByLabelText("Criterion name"),
      "State the positive claim",
    );
    await user.type(
      screen.getByLabelText("What should be true?"),
      "Prefer direct positive descriptions.",
    );
    await user.type(
      screen.getByLabelText("Proposed evaluation instructions"),
      "Identify negative-definition framing.",
    );
    await user.click(
      screen.getByRole("button", { name: "Save unavailable draft" }),
    );
    await waitFor(() => {
      expect(onBusyChange).toHaveBeenLastCalledWith(true);
    });

    await act(async () => {
      settleDraft();
    });
    await waitFor(() => {
      expect(onBusyChange).toHaveBeenLastCalledWith(false);
    });
  });

  it("saves a user-authored criterion only as an explicitly unavailable draft", async () => {
    const user = userEvent.setup();
    const onCreateDraft = vi.fn().mockResolvedValue(undefined);
    render(
      <VerifySetupCard
        capability={capability}
        configuration={configuration}
        onCreateDraft={onCreateDraft}
      />,
    );

    await user.click(screen.getByText("Verify setup"));
    await user.click(screen.getByText("Add a user-authored criterion"));
    expect(
      screen.getByText(/does not run a model, share document content, or admit/u),
    ).toBeVisible();
    await user.type(
      screen.getByLabelText("Criterion name"),
      "State the positive claim",
    );
    await user.type(
      screen.getByLabelText("What should be true?"),
      "Prefer direct positive descriptions.",
    );
    await user.type(
      screen.getByLabelText("Proposed evaluation instructions"),
      "Identify negative-definition framing.",
    );
    await user.type(
      screen.getByLabelText("Known limitation (optional)"),
      "Negation can be necessary.",
    );
    await user.click(
      screen.getByRole("button", { name: "Save unavailable draft" }),
    );

    expect(onCreateDraft).toHaveBeenCalledWith({
      title: "State the positive claim",
      description: "Prefer direct positive descriptions.",
      evaluationInstructions: "Identify negative-definition framing.",
      limitations: ["Negation can be necessary."],
    });
  });
});
