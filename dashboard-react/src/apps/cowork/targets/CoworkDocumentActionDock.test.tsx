import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import { DashboardHelpProvider } from "../../../dashboard/help";
import type { ChatExecutionControl } from "../../../widget-library/chat";
import {
  demoReviewData,
  InMemoryReviewProvider,
  type EvaluationRunSummary,
  type ReviewRailProvider,
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
  criteria: readonly VerificationCriterion[] = [criterion],
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
        criteria,
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
  projectionReceiptId: "projection-receipt-a",
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
        execution={executionControl()}
        storage={new MemoryStorage()}
      />,
    );

    const verify = screen.getByRole("button", { name: /^Verify/u });
    const cothink = screen.getByRole("button", { name: /^Co-think/u });
    expect(verify).toHaveAttribute("aria-expanded", "false");
    expect(cothink).toHaveAttribute("aria-expanded", "false");

    await user.click(verify);
    expect(verify).toHaveAttribute("aria-expanded", "true");
    expect(
      await screen.findByRole("region", { name: "Verification checks" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Run Verify" })).toBeVisible();
    expect(
      screen.queryByText(/Sessions ·|What should Verify accomplish|How checks/u),
    ).not.toBeInTheDocument();

    await user.click(cothink);
    expect(verify).toHaveAttribute("aria-expanded", "false");
    expect(cothink).toHaveAttribute("aria-expanded", "true");
    expect(
      within(screen.getByRole("region", { name: "Co-think" })).getByText(
        "Planned",
      ),
    ).toBeVisible();
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

  it("runs the selected checks against Working on without extra prompt fields", async () => {
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
    expect(
      screen.queryByRole("textbox", {
        name: "What should Verify accomplish?",
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", {
        name: "What must it preserve?",
      }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run Verify" }));

    await waitFor(() =>
      expect(onRunVerify).toHaveBeenCalledWith(capture, {
        userGoal:
          "Evaluate the current Working on target with the selected verification checks.",
        protectedIntent:
          "Preserve the author's intended meaning, voice, and constraints.",
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

  it("keeps provider mechanics out of the main run surface", async () => {
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
      screen.queryByRole("button", { name: /Run with Codex · GPT-5.6/u }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run Verify" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: /^Co-think/u }));
    expect(
      screen.queryByRole("button", { name: /Run with Codex · GPT-5.6/u }),
    ).not.toBeInTheDocument();
  });

  it("counts every enabled check and blocks Run when a selected check is unavailable", async () => {
    const user = userEvent.setup();
    const unavailableCriterion: VerificationCriterion = {
      ...criterion,
      id: "criterion-unavailable",
      stableKey: "legacy-unavailable",
      title: "Legacy unavailable check",
      definitionOrigin: "user",
      operationalState: "unavailable",
      availableCheckCount: 0,
      issues: [],
      checks: criterion.checks.map((check) => ({
        ...check,
        id: "check-unavailable",
        stableKey: "legacy-unavailable",
        availability: "unavailable",
        unavailableReason: "Its evaluator is not admitted.",
      })),
    };
    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={controller().value}
        reviewProvider={providerFor(
          "doc-a",
          [],
          executionPlan,
          [criterion, unavailableCriterion],
        )}
        onRunVerify={() => undefined}
        execution={executionControl()}
        storage={new MemoryStorage()}
      />,
    );

    const verify = screen.getByRole("button", { name: "Verify" });
    await waitFor(() =>
      expect(verify).toHaveAccessibleDescription("2 selected"),
    );
    await user.click(verify);
    expect(screen.getByRole("button", { name: "Run Verify" })).toBeDisabled();
    expect(screen.getByText("Turn off checks that need setup.")).toBeVisible();
    await user.click(screen.getByText("Checks"));
    expect(screen.getAllByText("2 selected")).toHaveLength(2);
    expect(
      screen.getByRole("checkbox", {
        name: "Legacy unavailable check: include in Verify runs",
      }),
    ).toBeChecked();
  });

  it("explains an unavailable execution model on the existing Run status surface", async () => {
    const user = userEvent.setup();
    render(
      <CoworkDocumentActionDock
        storeId="store-a"
        documentId="doc-a"
        controller={controller().value}
        reviewProvider={providerFor()}
        onRunVerify={() => undefined}
        execution={{
          ...executionControl(),
          snapshot: null,
          status: "unavailable",
          currentAvailable: false,
        }}
        storage={new MemoryStorage()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Verify" }));
    const run = screen.getByRole("button", { name: "Run Verify" });
    expect(run).toBeDisabled();
    expect(run).toHaveAccessibleDescription(
      "Verify needs an available account model.",
    );
    expect(
      screen.getByText("Verify needs an available account model."),
    ).toBeVisible();
  });

  it("replaces Run with Add check and preserves the draft across sibling toggles", async () => {
    const user = userEvent.setup();
    const provider = providerFor() as InMemoryReviewProvider & {
      createVerifyCheck: (
        check: unknown,
      ) => Promise<void>;
    };
    provider.createVerifyCheck = vi.fn(async () => undefined);
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
    await user.click(
      await screen.findByRole("button", { name: "Add check" }),
    );
    expect(
      screen.queryByRole("button", { name: "Run Verify" }),
    ).not.toBeInTheDocument();
    const name = screen.getByRole("textbox", { name: "Name" });
    await user.type(name, "Avoid negative definitions");

    await user.click(screen.getByRole("button", { name: /^Co-think/u }));
    expect(
      screen.queryByRole("textbox", { name: "Name" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Verify/u }));
    expect(
      screen.getByRole("textbox", { name: "Name" }),
    ).toHaveValue("Avoid negative definitions");
    await user.click(screen.getByRole("button", { name: "Close add check" }));
    expect(screen.getByRole("button", { name: "Run Verify" })).toBeVisible();
  });

  it("returns a saved personal check to the selected runnable menu", async () => {
    const user = userEvent.setup();
    const base = providerFor();
    let current = await base.load();
    const listeners = new Set<() => void>();
    const personalCriterion: VerificationCriterion = {
      ...criterion,
      id: "criterion-personal",
      stableKey: "state_positive_claim",
      title: "State the positive claim",
      description: "Prefer direct positive descriptions.",
      definitionOrigin: "user",
      author: { kind: "human", ref: "user", meta: null },
      activationId: "activation-personal",
      activationOrigin: "user",
      authorizedBy: { kind: "human", ref: "user", meta: null },
      checks: criterion.checks.map((check) => ({
        ...check,
        id: "check-personal",
        stableKey: "instruction_model_check",
        mechanism: "model_judge",
        executorRef: "system:instruction-model-check/v1",
        definitionOrigin: "system",
        dataSharingClass: "account_backed_agent",
        externalEgress: true,
        dataSharingBasis: "explicit_verify_run_selection",
        executionLocation: "account_backed_agent",
        bindingId: "binding-personal",
      })),
    };
    const provider: ReviewRailProvider = {
      load: async () => current,
      subscribe: (listener) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      submitSitting: base.submitSitting.bind(base),
      createVerifyCheck: async () => {
        current = {
          ...current,
          verificationConfiguration: {
            ...current.verificationConfiguration,
            criteria: [
              ...current.verificationConfiguration.criteria,
              personalCriterion,
            ],
          },
        };
        for (const listener of listeners) listener();
      },
    };
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

    await user.click(screen.getByRole("button", { name: "Verify" }));
    await user.click(await screen.findByRole("button", { name: "Add check" }));
    await user.type(
      screen.getByRole("textbox", { name: "Name" }),
      "State the positive claim",
    );
    await user.type(
      screen.getByRole("textbox", { name: "What should it check?" }),
      "Prefer direct positive descriptions.",
    );
    await user.click(screen.getByRole("button", { name: "Save check" }));

    expect(
      await screen.findByRole("button", { name: "Run Verify" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Verify" }),
    ).toHaveAccessibleDescription("2 selected");
    await user.click(screen.getByText("Checks"));
    expect(await screen.findByText("State the positive claim")).toBeVisible();
    expect(screen.getByText("Yours")).toBeVisible();
    expect(
      screen.getByRole("checkbox", {
        name: "State the positive claim: include in Verify runs",
      }),
    ).toBeChecked();
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
    expect(
      screen.getByText("Loading checks…"),
    ).toBeVisible();
    expect(
      screen.queryByText(/unattested|execution disclosure/u),
    ).not.toBeInTheDocument();
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
      screen.queryByText(/checker egress|sessions ·|provider\/model fallback/i),
    ).not.toBeInTheDocument();
  });

  it("attaches Hover help to the existing sibling headers", async () => {
    const user = userEvent.setup();
    render(
      <DashboardHelpProvider enabled>
        <CoworkDocumentActionDock
          storeId="store-a"
          documentId="doc-a"
          controller={controller().value}
          reviewProvider={providerFor()}
          onRunVerify={() => undefined}
          execution={executionControl()}
          storage={new MemoryStorage()}
        />
      </DashboardHelpProvider>,
    );

    const verify = screen.getByRole("button", { name: "Verify" });
    const cothink = screen.getByRole("button", { name: "Co-think" });
    await waitFor(() =>
      expect(verify).toHaveAccessibleDescription("1 selected"),
    );
    expect(cothink).toHaveAccessibleDescription("Planned");
    expect(verify).toHaveAttribute("data-help-target", "true");
    expect(cothink).toHaveAttribute("data-help-target", "true");
    expect(screen.queryByText(/^How /u)).not.toBeInTheDocument();

    await user.hover(verify);
    expect(
      await screen.findByText(
        "Choose checks and run them against Working on.",
      ),
    ).toBeVisible();
    await user.unhover(verify);
    await user.hover(cothink);
    expect(
      await screen.findByText(
        "Challenge or explore the work from another angle.",
      ),
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

    await user.click(screen.getByText("Checks"));
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

    expect(await screen.findByText(/Recheck\./u)).toBeVisible();
    expect(screen.queryByText("Original run model · Codex · GPT-5.6"))
      .not.toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", {
        name: "What should Verify accomplish?",
      }),
    ).not.toBeInTheDocument();
    const runVerify = await screen.findByRole("button", {
      name: "Run Verify",
    });
    expect(runVerify).toBeDisabled();
    await user.click(
      screen.getByRole("button", {
        name: "Use this passage",
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
      await screen.findByRole("button", {
        name: "Use this passage",
      }),
    );
    const runVerify = await screen.findByRole("button", {
      name: "Run Verify",
    });
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
        "Original passage: Original methods passage.",
      ),
    ).toBeVisible();
    expect(onRunVerify).not.toHaveBeenCalled();
    expect(fake.captureReference).not.toHaveBeenCalled();

    await user.click(
      await screen.findByRole("button", { name: "Run Verify" }),
    );
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
