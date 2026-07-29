import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import type { ChatExecutionControl } from "../../../widget-library/chat";
import {
  demoReviewData,
  InMemoryReviewProvider,
  type EvaluationRunSummary,
} from "../rail";
import type {
  VerificationCriterion,
  VerificationRecheckIntent,
  VerifyExecutionPlan,
} from "../rail/contracts";
import { CoworkDocumentActionDock } from "./CoworkDocumentActionDock";
import { coworkTargetReferenceIdentitySha256 } from "./targetReferenceIdentity";
import type {
  CoworkActionSnapshotController,
  CoworkActionSnapshotControllerState,
  CoworkCapturedActionSnapshot,
} from "./contracts";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

const criterion: VerificationCriterion = {
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
};

const run: EvaluationRunSummary = {
  runId: "run-1",
  status: "completed",
  purpose: "document_review",
  targetLabel: "Methods",
  coverageLabel: "Complete exact-string scan",
  currentVersion: true,
  resultCount: 1,
  surfacedResultCount: 0,
  coordinationStatus: "completed",
  providerLabel: "Codex",
  providerId: "codex",
  modelLabel: "GPT-5.6",
  modelId: "gpt-5.6",
  createdAt: "2026-07-28T00:00:00Z",
  finishedAt: "2026-07-28T00:00:01Z",
};

const executionPlan: VerifyExecutionPlan = {
  schema: "work-buddy.cowork-verify-execution-disclosure/v1",
  authoritative: true,
  checker: {
    executionClass: "in_process",
    mechanism: "deterministic_exact_match",
    modelCall: false,
    externalEgress: false,
    contentBoundary: "captured_target",
  },
  coordination: {
    executionClass: "account_backed_agent",
    selection: {
      mode: "explicit_at_run_start",
      providerId: "codex",
      modelId: "gpt-5.6",
      providerLabel: "Codex",
      modelLabel: "GPT-5.6",
    },
    contentBoundary: "entire_frozen_document",
    externalEgress: true,
    fallback: {
      providerModelFallback: false,
      failureMode: "fail_closed",
    },
    workerSessions: {
      initial: 2,
      maximum: 5,
      conditionalRoles: ["reviser", "post_revision_coordinator"],
    },
    costControl: {
      providerId: "codex",
      enforcementClass: "estimate",
      ceilingUsdPerWorkerSession: 1.25,
      basis: "test_attestation",
    },
    providerCostControls: [
      {
        providerId: "codex",
        enforcementClass: "estimate",
        ceilingUsdPerWorkerSession: 1.25,
        basis: "test_attestation",
      },
    ],
  },
};

const providerFor = (
  documentId = "doc-a",
  runs: readonly EvaluationRunSummary[] = [],
  plan: VerifyExecutionPlan | null = executionPlan,
): InMemoryReviewProvider => {
  const data = demoReviewData();
  return new InMemoryReviewProvider({
    data: {
      ...data,
      documentId,
      verificationConfiguration: {
        ...data.verificationConfiguration,
        documentId,
        executionPlan: plan,
        criteria: [criterion],
      },
      evaluationRuns: runs,
    },
  });
};

const targetState: CoworkActionSnapshotControllerState = {
  phase: "ready",
  selection: {
    kind: "text_range",
    label: "Risks",
    wordCount: 12,
    range: { from: 4, to: 30 },
  },
  currentSection: {
    kind: "text_range",
    label: "Methods",
    wordCount: 85,
    range: { from: 2, to: 140 },
  },
  workingTarget: {
    kind: "text_range",
    label: "Methods",
    wordCount: 42,
    range: { from: 150, to: 240 },
    resolution: "relative",
  },
  workingTargetStart: null,
};

const capture: CoworkCapturedActionSnapshot = {
  schema: "wb.cowork.action-snapshot/v1",
  captureId: "capture-a",
  storeId: "store-a",
  documentId: "doc-a",
  capturedAt: "2026-07-28T12:00:00.000Z",
  editGeneration: 1,
  ydocGenerationSha256: "generation-a",
  snapshotBase64: "AA==",
  snapshotSha256: "snapshot",
  stateVectorBase64: "AQ==",
  stateVectorSha256: "state-vector",
  structuredHeadSha256: "head",
  projectionMarkdown: "# Methods",
  projectionSha256: "projection",
  target: {
    source: "working_target",
    label: "Methods",
    wordCount: 42,
    proseMirrorRange: { from: 150, to: 240 },
    selector: {
      kind: "text_quote",
      exact: "# Methods",
      prefix: "",
      suffix: "",
      start: 0,
      end: 9,
    },
    targetTextSha256: "target",
    targetReference: {
      schema: "wb.cowork.document-target/v1",
      storeId: "store-a",
      documentId: "doc-a",
      kind: "text_range",
      granularity: "character",
      relative: {
        startBase64: "AA==",
        endBase64: "AQ==",
      },
      quote: {
        exact: "# Methods",
        prefix: "",
        suffix: "",
      },
      label: "Methods",
      headingPath: ["Methods"],
      createdAt: "2026-07-28T00:00:00Z",
      updatedAt: "2026-07-28T00:00:01Z",
    },
  },
};

const controller = (): {
  readonly value: CoworkActionSnapshotController;
  readonly capture: ReturnType<typeof vi.fn>;
  readonly captureReference: ReturnType<typeof vi.fn>;
} => {
  const captureAction = vi.fn(async () => capture);
  const captureReferenceAction = vi.fn(async () => capture);
  return {
    value: {
      getSnapshot: () => targetState,
      subscribe: () => () => undefined,
      setWorkingTargetFromSelection: vi.fn(),
      clearWorkingTarget: vi.fn(),
      capture: captureAction,
      captureReference: captureReferenceAction,
    },
    capture: captureAction,
    captureReference: captureReferenceAction,
  };
};

const executionControl = (): ChatExecutionControl => ({
  snapshot: {
    selection: {
      providerId: "codex",
      modelId: "gpt-5.6",
      providerLabel: "Codex",
      modelLabel: "GPT-5.6",
      revision: "1",
    },
    providers: [
      {
        id: "codex",
        label: "Codex",
        available: true,
        models: [
          {
            id: "gpt-5.6",
            label: "GPT-5.6",
            available: true,
          },
        ],
      },
    ],
  },
  status: "ready",
  selecting: false,
  error: null,
  announcement: null,
  currentAvailable: true,
  select: vi.fn(async () => undefined),
  retry: vi.fn(),
});

const legacyRecheck: VerificationRecheckIntent = {
  intentId: "recheck-legacy",
  sittingId: "sitting-1",
  sourceRunId: "run-original",
  proposalIds: ["proposal-1"],
  pendingProposalIds: ["proposal-1"],
  fulfilledByRunIds: [],
  committedAt: "2026-07-28T00:00:02Z",
  status: "user_action_required",
  userGoal: "Recheck the applied terminology correction.",
  protectedIntent: "Preserve the author’s substantive meaning.",
  originalActionTarget: {
    actionSnapshotId: "action-original",
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
    targetTextSha256: "a".repeat(64),
    targetReference: null,
    targetReferenceSha256: null,
  },
  execution: {
    providerId: "codex",
    modelId: "gpt-5.6",
    providerLabel: "Codex",
    modelLabel: "GPT-5.6",
  },
  requires: {
    freshActionSnapshot: true,
    freshModelCallAuthorization: true,
    sameTargetSource: false,
    sameTargetReference: false,
    exactTargetResolution: false,
    userAffirmedExactTargetRequired: true,
    allowWidenToWholeDocument: false,
  },
};

const durableRecheck: VerificationRecheckIntent = {
  ...legacyRecheck,
  intentId: "recheck-durable",
  status: "pending_capture",
  requires: {
    ...legacyRecheck.requires,
    sameTargetSource: true,
    sameTargetReference: true,
    exactTargetResolution: true,
    userAffirmedExactTargetRequired: false,
  },
  originalActionTarget: {
    ...legacyRecheck.originalActionTarget,
    source: "working_target",
    label: "Original methods passage",
    targetReference: {
      schema: "wb.cowork.document-target/v1",
      storeId: "store-a",
      documentId: "doc-a",
      kind: "text_range",
      granularity: "character",
      relative: {
        startBase64: "AA==",
        endBase64: "AQ==",
      },
      quote: {
        exact: "Original methods passage",
        prefix: "",
        suffix: "",
      },
      label: "Original methods passage",
      headingPath: ["Methods"],
      createdAt: "2026-07-28T00:00:00Z",
      updatedAt: "2026-07-28T00:00:01Z",
    },
    targetReferenceSha256: "b".repeat(64),
  },
};

describe("CoworkDocumentActionDock", () => {
  it("keeps Verify and planned Co-think as one-open sibling panels", async () => {
    const user = userEvent.setup();
    const fake = controller();
    const { container } = render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={fake.value}
        reviewProvider={providerFor("doc-a", [run])}
        onRunVerify={() => undefined}
        storage={new MemoryStorage()}
      />,
    );

    const verify = screen.getByRole("button", { name: /^Verify/u });
    const cothink = screen.getByRole("button", { name: /^Co-think/u });
    expect(verify).toHaveAttribute("aria-expanded", "false");
    expect(cothink).toHaveAttribute("aria-expanded", "false");

    await user.click(verify);
    expect(verify).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("Verify runs")).toBeVisible();
    expect(screen.queryByText("Custom range")).not.toBeInTheDocument();

    await user.click(cothink);
    expect(verify).toHaveAttribute("aria-expanded", "false");
    expect(cothink).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Planned")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Invite perspective" }),
    ).not.toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("remembers the open panel independently for each document", async () => {
    const user = userEvent.setup();
    const storage = new MemoryStorage();
    const first = controller();
    const rendered = render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={first.value}
        reviewProvider={providerFor("doc-a")}
        storage={storage}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^Verify/u }));
    rendered.unmount();

    const restored = render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={controller().value}
        reviewProvider={providerFor("doc-a")}
        storage={storage}
      />,
    );
    expect(
      screen.getByRole("button", { name: /^Verify/u }),
    ).toHaveAttribute("aria-expanded", "true");
    restored.unmount();

    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-b"
        controller={controller().value}
        reviewProvider={providerFor("doc-b")}
        storage={storage}
      />,
    );
    expect(
      screen.getByRole("button", { name: /^Verify/u }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it("captures one exact target and passes the edited goal and protected intent", async () => {
    const user = userEvent.setup();
    const fake = controller();
    const onRunVerify = vi.fn(async () => undefined);
    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={fake.value}
        reviewProvider={providerFor()}
        onRunVerify={onRunVerify}
        execution={executionControl()}
        storage={new MemoryStorage()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^Verify/u }));
    await screen.findByRole("button", { name: "Run Verify" });
    const goal = screen.getByRole("textbox", {
      name: "What should Verify accomplish?",
    });
    const protectedIntent = screen.getByRole("textbox", {
      name: "What must it preserve?",
    });
    await user.clear(goal);
    await user.type(goal, "Keep the PRD faithful to the requested workflow.");
    await user.clear(protectedIntent);
    await user.type(protectedIntent, "Do not change the product boundary.");
    await user.click(screen.getByRole("button", { name: "Run Verify" }));

    await waitFor(() =>
      expect(onRunVerify).toHaveBeenCalledWith(capture, {
        userGoal: "Keep the PRD faithful to the requested workflow.",
        protectedIntent: "Do not change the product boundary.",
        execution: {
          providerId: "codex",
          modelId: "gpt-5.6",
          providerLabel: "Codex",
          modelLabel: "GPT-5.6",
        },
      }),
    );
    expect(fake.capture).toHaveBeenCalledWith("working_target");
  });

  it("renders a Verify-local execution picker only inside Verify", async () => {
    const user = userEvent.setup();
    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={controller().value}
        reviewProvider={providerFor()}
        execution={executionControl()}
        storage={new MemoryStorage()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^Verify/u }));
    expect(
      screen.getByRole("button", { name: /Run with Codex · GPT-5.6/u }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: /^Co-think/u }));
    expect(
      screen.queryByRole("button", { name: /Run with Codex · GPT-5.6/u }),
    ).not.toBeInTheDocument();
  });

  it("keeps an unsaved criterion draft mounted across sibling-panel toggles", async () => {
    const user = userEvent.setup();
    const provider = providerFor() as InMemoryReviewProvider & {
      createVerifyCriterionDraft: (
        draft: unknown,
      ) => Promise<void>;
    };
    provider.createVerifyCriterionDraft = vi.fn(async () => undefined);
    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={controller().value}
        reviewProvider={provider}
        execution={executionControl()}
        storage={new MemoryStorage()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^Verify/u }));
    await user.click(await screen.findByText("Verify setup"));
    await user.click(screen.getByText("Add a user-authored criterion"));
    const name = screen.getByRole("textbox", { name: "Criterion name" });
    await user.type(name, "Avoid negative definitions");

    await user.click(screen.getByRole("button", { name: /^Co-think/u }));
    expect(
      screen.queryByRole("textbox", { name: "Criterion name" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Verify/u }));
    expect(
      screen.getByRole("textbox", { name: "Criterion name" }),
    ).toHaveValue("Avoid negative definitions");
  });

  it("fails closed without matching ready review data and an authoritative plan", async () => {
    const user = userEvent.setup();
    const mismatched = render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={controller().value}
        reviewProvider={providerFor("doc-b")}
        onRunVerify={() => undefined}
        execution={executionControl()}
        storage={new MemoryStorage()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^Verify/u }));
    expect(screen.getByRole("button", { name: "Run Verify" })).toBeDisabled();
    expect(
      screen.getByText(
        "Execution disclosure · unknown/unattested for this document",
      ),
    ).toBeVisible();
    mismatched.unmount();

    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={controller().value}
        reviewProvider={providerFor("doc-a", [], null)}
        onRunVerify={() => undefined}
        execution={executionControl()}
        storage={new MemoryStorage()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^Verify/u }));
    expect(screen.getByRole("button", { name: "Run Verify" })).toBeDisabled();
    expect(
      screen.queryByText(/no checker egress|up to 3|no provider\/model fallback/i),
    ).not.toBeInTheDocument();
  });

  it("renders execution, sharing, session, fallback, and cost claims from the plan", async () => {
    const user = userEvent.setup();
    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={controller().value}
        reviewProvider={providerFor()}
        onRunVerify={() => undefined}
        execution={executionControl()}
        storage={new MemoryStorage()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^Verify/u }));

    expect(
      screen.getByText(/Checker · captured target · in process/u),
    ).toBeVisible();
    expect(
      screen.getByText(/Sessions · 2 initial · 5 maximum/u),
    ).toBeVisible();
    expect(
      screen.getByText(/Cost control · estimate · \$1.25 per worker session/u),
    ).toBeVisible();
    expect(
      screen.getByText(/no provider\/model fallback · fail closed/u),
    ).toBeVisible();
  });

  it("disables Run while Verify setup is awaiting authoritative mutation state", async () => {
    const user = userEvent.setup();
    let releaseMutation: (() => void) | undefined;
    const mutation = new Promise<void>((resolve) => {
      releaseMutation = resolve;
    });
    const provider = providerFor() as InMemoryReviewProvider & {
      setVerifyCriterionEnabled: (
        criterionKey: string,
        enabled: boolean,
        expectedActivationId: string | null,
      ) => Promise<void>;
    };
    provider.setVerifyCriterionEnabled = vi.fn(() => mutation);
    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={controller().value}
        reviewProvider={provider}
        onRunVerify={() => undefined}
        execution={executionControl()}
        storage={new MemoryStorage()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^Verify/u }));
    const runButton = screen.getByRole("button", { name: "Run Verify" });
    expect(runButton).toBeEnabled();

    await user.click(
      screen.getByRole("checkbox", {
        name: "Preferred terminology: include in Verify runs",
      }),
    );
    await waitFor(() => expect(runButton).toBeDisabled());
    releaseMutation?.();
    await waitFor(() => expect(runButton).toBeEnabled());
  });

  it("launches a target-required recheck with its exact original execution and lineage", async () => {
    const user = userEvent.setup();
    const fake = controller();
    const runCapture = {
      ...capture,
      captureId: "capture-run-a",
    } satisfies CoworkCapturedActionSnapshot;
    fake.capture
      .mockResolvedValueOnce(capture)
      .mockResolvedValueOnce(runCapture);
    const onRunVerify = vi.fn(async () => undefined);
    const onAffirmRecheckTarget = vi.fn(async (affirmedCapture) => ({
      schema:
        "work-buddy.cowork-recheck-target-affirmation-receipt/v1" as const,
      recheckIntentId: legacyRecheck.intentId,
      sourceRunId: legacyRecheck.sourceRunId,
      pendingProposalIds: legacyRecheck.pendingProposalIds,
      affirmedCaptureId: affirmedCapture.captureId,
      affirmedActionSnapshotId: "affirmed-action-1",
      targetReferenceSha256:
        await coworkTargetReferenceIdentitySha256(
          affirmedCapture.target.targetReference!,
        ),
      targetTextSha256: affirmedCapture.target.targetTextSha256,
      affirmedAt: "2026-07-28T00:00:02Z",
    }));
    const clearRecheck = vi.fn();
    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={fake.value}
        reviewProvider={providerFor()}
        onRunVerify={onRunVerify}
        onAffirmRecheckTarget={onAffirmRecheckTarget}
        execution={executionControl()}
        armedRecheck={legacyRecheck}
        onClearArmedRecheck={clearRecheck}
        storage={new MemoryStorage()}
      />,
    );

    expect(
      await screen.findByText(/Bound recheck\./u),
    ).toBeVisible();
    expect(
      screen.getByLabelText("Bound recheck execution"),
    ).toHaveTextContent("Original run model · Codex · GPT-5.6");
    expect(
      screen.getByRole("textbox", {
        name: "What should Verify accomplish?",
      }),
    ).toBeDisabled();
    const runVerify = screen.getByRole("button", { name: "Run Verify" });
    expect(runVerify).toBeDisabled();
    await user.click(
      screen.getByRole("button", {
        name: "Use this Working on passage",
      }),
    );
    await waitFor(() => expect(runVerify).toBeEnabled());
    await user.click(runVerify);

    await waitFor(() =>
      expect(onRunVerify).toHaveBeenCalledWith(
        runCapture,
        expect.objectContaining({
          userGoal: legacyRecheck.userGoal,
          protectedIntent: legacyRecheck.protectedIntent,
          execution: legacyRecheck.execution,
          recheck: {
            intentId: legacyRecheck.intentId,
            sourceRunId: legacyRecheck.sourceRunId,
            pendingProposalIds: legacyRecheck.pendingProposalIds,
            targetConfirmation: {
              schema:
                "work-buddy.cowork-recheck-target-confirmation/v1",
              method: "user_affirmed_working_target",
              affirmedCaptureId: capture.captureId,
              affirmedActionSnapshotId: "affirmed-action-1",
              runCaptureId: runCapture.captureId,
              targetReferenceSha256: expect.stringMatching(
                /^[a-f0-9]{64}$/u,
              ),
              targetTextSha256: capture.target.targetTextSha256,
            },
          },
        }),
      ),
    );
    expect(fake.capture).toHaveBeenCalledTimes(2);
    expect(fake.capture).toHaveBeenNthCalledWith(1, "working_target");
    expect(fake.capture).toHaveBeenNthCalledWith(2, "working_target");
    expect(onAffirmRecheckTarget).toHaveBeenCalledWith(
      capture,
      expect.objectContaining({ intentId: legacyRecheck.intentId }),
    );
    expect(clearRecheck).toHaveBeenCalledTimes(1);
  });

  it("requires renewed affirmation when the target text changes inside the same range", async () => {
    const user = userEvent.setup();
    const fake = controller();
    const changedCapture: CoworkCapturedActionSnapshot = {
      ...capture,
      captureId: "capture-after-edit",
      target: {
        ...capture.target,
        selector: {
          kind: "text_quote",
          exact: "# Methodz",
          prefix: "",
          suffix: "",
          start: 0,
          end: 9,
        },
        targetTextSha256: "changed-target",
      },
    };
    fake.capture
      .mockResolvedValueOnce(capture)
      .mockResolvedValueOnce(changedCapture);
    const onRunVerify = vi.fn(async () => undefined);
    const onAffirmRecheckTarget = vi.fn(async (affirmedCapture) => ({
      schema:
        "work-buddy.cowork-recheck-target-affirmation-receipt/v1" as const,
      recheckIntentId: legacyRecheck.intentId,
      sourceRunId: legacyRecheck.sourceRunId,
      pendingProposalIds: legacyRecheck.pendingProposalIds,
      affirmedCaptureId: affirmedCapture.captureId,
      affirmedActionSnapshotId: "affirmed-action-2",
      targetReferenceSha256:
        await coworkTargetReferenceIdentitySha256(
          affirmedCapture.target.targetReference!,
        ),
      targetTextSha256: affirmedCapture.target.targetTextSha256,
      affirmedAt: "2026-07-28T00:00:02Z",
    }));
    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={fake.value}
        reviewProvider={providerFor()}
        onRunVerify={onRunVerify}
        onAffirmRecheckTarget={onAffirmRecheckTarget}
        execution={executionControl()}
        armedRecheck={legacyRecheck}
        storage={new MemoryStorage()}
      />,
    );

    await user.click(
      screen.getByRole("button", {
        name: "Use this Working on passage",
      }),
    );
    const runVerify = screen.getByRole("button", { name: "Run Verify" });
    await waitFor(() => expect(runVerify).toBeEnabled());
    await user.click(runVerify);

    expect(
      await screen.findByRole("alert"),
    ).toHaveTextContent(
      "Working on changed after it was affirmed. Review and affirm the exact passage again.",
    );
    expect(onRunVerify).not.toHaveBeenCalled();
    expect(runVerify).toBeDisabled();
  });

  it("waits for Run Verify before capturing a durable original recheck target", async () => {
    const user = userEvent.setup();
    const fake = controller();
    const onRunVerify = vi.fn(async () => undefined);
    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={fake.value}
        reviewProvider={providerFor()}
        onRunVerify={onRunVerify}
        execution={executionControl()}
        armedRecheck={durableRecheck}
        onClearArmedRecheck={() => undefined}
        storage={new MemoryStorage()}
      />,
    );

    expect(
      await screen.findByText(
        "Original target · Original methods passage",
      ),
    ).toBeVisible();
    expect(onRunVerify).not.toHaveBeenCalled();
    expect(fake.captureReference).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Run Verify" }));
    await waitFor(() =>
      expect(fake.captureReference).toHaveBeenCalledWith(
        "working_target",
        durableRecheck.originalActionTarget.targetReference,
      ),
    );
    expect(onRunVerify).toHaveBeenCalledWith(
      capture,
      expect.objectContaining({
        execution: durableRecheck.execution,
        recheck: {
          intentId: durableRecheck.intentId,
          sourceRunId: durableRecheck.sourceRunId,
          pendingProposalIds: durableRecheck.pendingProposalIds,
        },
      }),
    );
    expect(fake.capture).not.toHaveBeenCalled();
  });
});
