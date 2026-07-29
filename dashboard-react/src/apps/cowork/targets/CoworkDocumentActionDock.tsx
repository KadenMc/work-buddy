import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { HelpTarget, type HelpContent } from "../../../dashboard/help";
import { Button, SelectField, TextAreaField } from "../../../ui";
import {
  ChatExecutionPicker,
  type ChatExecutionControl,
} from "../../../widget-library/chat";
import {
  VerifySetupCard,
  useReviewData,
  type CoworkVerifyCapability,
  type EvaluationRunSummary,
  type ReviewRailProvider,
} from "../rail";
import type {
  VerificationRecheckIntent,
  VerifyRunInspection,
} from "../rail/contracts";
import type {
  CoworkActionSnapshotController,
  CoworkActionSnapshotControllerState,
  CoworkActionTargetChoice,
  CoworkAffirmVerifyRecheckTargetHandler,
  CoworkCapturedActionSnapshot,
  CoworkRunVerifyHandler,
  CoworkVerifyExecutionSelection,
  CoworkVerifyRecheckTargetAffirmationReceipt,
  CoworkVerifyRecheckTargetConfirmation,
} from "./contracts";
import { coworkTargetReferenceIdentitySha256 } from "./targetReferenceIdentity";
import "./styles.css";

type DockPanel = "verify" | "cothink";

interface AffirmedWorkingTarget {
  readonly workingTargetKey: string;
  readonly receipt: CoworkVerifyRecheckTargetAffirmationReceipt;
}

const DOCK_STORAGE_PREFIX = "wb.cowork.action-dock.v1";
const DEFAULT_VERIFY_GOAL =
  "Check this target against the active verification criteria.";
const DEFAULT_PROTECTED_INTENT =
  "Preserve the author's intended meaning, voice, and constraints.";
const VERIFY_EXECUTION_PLAN_SCHEMA =
  "work-buddy.cowork-verify-execution-disclosure/v1";

const FALLBACK_STATE: CoworkActionSnapshotControllerState = {
  phase: "loading",
  selection: null,
  currentSection: null,
  workingTarget: {
    kind: "document",
    label: "Whole document",
    wordCount: 0,
    range: null,
  },
};

const RUN_LABEL: Readonly<Record<EvaluationRunSummary["status"], string>> = {
  prepared: "Preparing",
  queued: "Queued",
  running: "Checking",
  completed: "Complete",
  completed_with_failures: "Completed with limits",
  failed: "Couldn’t complete",
  cancelled: "Cancelled",
};

const subscribeFallback = (): (() => void) => () => undefined;
const getFallback = (): CoworkActionSnapshotControllerState => FALLBACK_STATE;

const planValue = (value: unknown): string => {
  if (typeof value === "string" && value.trim().length > 0) {
    return value.replace(/_/gu, " ");
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return value.toLocaleString();
  }
  return "unattested";
};

const planToggle = (
  value: unknown,
  whenTrue: string,
  whenFalse: string,
): string =>
  value === true ? whenTrue : value === false ? whenFalse : "unattested";

const rawPlanValue = (value: unknown): string => {
  if (typeof value === "string" && value.trim().length > 0) return value;
  if (typeof value === "number" && Number.isFinite(value)) {
    return value.toString();
  }
  if (typeof value === "boolean") return value.toString();
  return "unattested";
};

const actionErrorMessage = (error: unknown): string =>
  error instanceof Error
    ? error.message
    : "Co-work Verify could not capture this document version.";

const storageKey = (storeId: string, documentId: string): string =>
  `${DOCK_STORAGE_PREFIX}:${encodeURIComponent(storeId)}:${encodeURIComponent(documentId)}`;

const loadDockPanel = (
  storage: Storage | undefined,
  storeId: string,
  documentId: string,
): DockPanel | null => {
  const value = storage?.getItem(storageKey(storeId, documentId));
  return value === "verify" || value === "cothink" ? value : null;
};

const saveDockPanel = (
  storage: Storage | undefined,
  storeId: string,
  documentId: string,
  panel: DockPanel | null,
): void => {
  const key = storageKey(storeId, documentId);
  if (panel === null) storage?.removeItem(key);
  else storage?.setItem(key, panel);
};

const latestFirst = <T extends { readonly createdAt: string }>(
  values: readonly T[],
): readonly T[] =>
  [...values].sort((left, right) =>
    right.createdAt.localeCompare(left.createdAt),
  );

function VerifyRunInspectionView({
  inspection,
}: {
  readonly inspection: VerifyRunInspection;
}) {
  return (
    <div className="wb-cowork-verify-history__inspection">
      <p>
        Frozen plan <code>{inspection.plan.planSnapshotId.slice(0, 12)}</code> ·
        document version{" "}
        <code>{inspection.action.structuredHeadSha256.slice(0, 12)}</code>
      </p>
      <ul>
        {inspection.checks.map((check) => (
          <li key={check.checkExecutionId}>
            {check.definition.title} v{check.definition.version.toString()} ·{" "}
            {check.mechanism} · {check.status}
          </li>
        ))}
      </ul>
      {inspection.coordination.length > 0 ? (
        <ul>
          {inspection.coordination.map((job) => (
            <li key={job.jobId}>
              {job.role.replace(/_/gu, " ")} · {job.provider} · {job.model} ·{" "}
              {job.status} · requested launch budget $
              {job.costCeilingUsd.toFixed(2)}
              {job.error === null ? "" : ` · ${job.error}`}
            </li>
          ))}
        </ul>
      ) : null}
      {inspection.results.map((result) => (
        <div key={result.evaluationResultId}>
          <strong>{result.kind.replace(/_/gu, " ")}</strong>
          <p>{result.message}</p>
          {result.dispositions.map((disposition, index) => (
            <p key={`${disposition.decision}:${index.toString()}`}>
              {disposition.decision.replace(/_/gu, " ")} ·{" "}
              {disposition.rationale}
            </p>
          ))}
          {result.lineage.length > 0 ? (
            <p>
              Lineage:{" "}
              {result.lineage
                .map(
                  (relation) =>
                    `${relation.relation} ${relation.targetKind} ${relation.targetRef.slice(0, 12)}`,
                )
                .join(" · ")}
            </p>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export function VerifyRunHistory({
  runs,
  onInspectRun,
}: {
  readonly runs: readonly EvaluationRunSummary[];
  readonly onInspectRun?: (runId: string) => Promise<VerifyRunInspection>;
}) {
  const [inspection, setInspection] = useState<VerifyRunInspection | null>(
    null,
  );
  const [loadingRunId, setLoadingRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (runs.length === 0) return null;
  const latest = latestFirst(runs)[0];
  const active =
    latest.status === "prepared" ||
    latest.status === "queued" ||
    latest.status === "running";
  return (
    <details
      className="wb-cowork-verify-history"
      open={active || inspection !== null}
    >
      <summary>
        <span>Verify runs</span>
        <span className="wb-cowork-verify-history__summary">
          {RUN_LABEL[latest.status]} · {latest.targetLabel}
        </span>
      </summary>
      <ol className="wb-cowork-verify-history__list">
        {latestFirst(runs).map((run) => (
          <li key={run.runId}>
            <span className="wb-cowork-verify-history__status">
              {RUN_LABEL[run.status]}
            </span>
            <span>{run.targetLabel}</span>
            <span>{run.coverageLabel}</span>
            {!run.currentVersion ? <span>Earlier version</span> : null}
            {run.coordinationStatus === "unavailable" ? (
              <span role="status">Coordination unavailable</span>
            ) : null}
            {onInspectRun !== undefined ? (
              <button
                type="button"
                disabled={loadingRunId !== null}
                onClick={() => {
                  setLoadingRunId(run.runId);
                  setError(null);
                  void onInspectRun(run.runId)
                    .then(
                      (value) => setInspection(value),
                      (cause: unknown) =>
                        setError(
                          cause instanceof Error
                            ? cause.message
                            : "Verify run details could not be loaded.",
                        ),
                    )
                    .finally(() => setLoadingRunId(null));
                }}
              >
                {loadingRunId === run.runId ? "Loading…" : "Inspect"}
              </button>
            ) : null}
          </li>
        ))}
      </ol>
      {inspection !== null ? (
        <VerifyRunInspectionView inspection={inspection} />
      ) : null}
      {error !== null ? <p role="alert">{error}</p> : null}
    </details>
  );
}

export interface CoworkDocumentActionDockProps {
  readonly storeId: string;
  readonly documentId: string;
  readonly controller: CoworkActionSnapshotController | null;
  readonly reviewProvider: ReviewRailProvider;
  readonly readOnly?: boolean;
  readonly onRunVerify?: CoworkRunVerifyHandler;
  readonly onAffirmRecheckTarget?: CoworkAffirmVerifyRecheckTargetHandler;
  readonly verifySetup?: {
    readonly activeCount: number;
    readonly unavailableCount: number;
  } | null;
  readonly verifyCapability?: CoworkVerifyCapability | null;
  /** Verify-local execution selection; changing it must not restart Chat. */
  readonly execution?: ChatExecutionControl;
  /**
   * A durable legacy recheck whose exact earlier text target cannot be
   * recovered. The dock may capture only a newly affirmed Working on passage.
   */
  readonly armedRecheck?: VerificationRecheckIntent | null;
  readonly onClearArmedRecheck?: () => void;
  /** Injectable for focused tests. */
  readonly storage?: Storage;
}

export function CoworkDocumentActionDock({
  storeId,
  documentId,
  controller,
  reviewProvider,
  readOnly = false,
  onRunVerify,
  onAffirmRecheckTarget,
  verifySetup,
  verifyCapability,
  execution,
  armedRecheck = null,
  onClearArmedRecheck,
  storage = typeof window === "undefined" ? undefined : window.localStorage,
}: CoworkDocumentActionDockProps) {
  const targetState = useSyncExternalStore(
    controller?.subscribe ?? subscribeFallback,
    controller?.getSnapshot ?? getFallback,
    getFallback,
  );
  const { data, status: reviewStatus, error: reviewError, reload } =
    useReviewData(reviewProvider);
  const [expanded, setExpanded] = useState<DockPanel | null>(() =>
    loadDockPanel(storage, storeId, documentId),
  );
  const [choice, setChoice] =
    useState<CoworkActionTargetChoice>("working_target");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [verifyGoal, setVerifyGoal] = useState(DEFAULT_VERIFY_GOAL);
  const [protectedIntent, setProtectedIntent] = useState(
    DEFAULT_PROTECTED_INTENT,
  );
  const [setupBusy, setSetupBusy] = useState(false);
  const [affirmedWorkingTarget, setAffirmedWorkingTarget] =
    useState<AffirmedWorkingTarget | null>(null);
  const normalRunStateRef = useRef({
    choice: "working_target" as CoworkActionTargetChoice,
    verifyGoal: DEFAULT_VERIFY_GOAL,
    protectedIntent: DEFAULT_PROTECTED_INTENT,
  });

  useEffect(() => {
    setExpanded(loadDockPanel(storage, storeId, documentId));
    setChoice("working_target");
    setMessage(null);
    setError(null);
    setVerifyGoal(DEFAULT_VERIFY_GOAL);
    setProtectedIntent(DEFAULT_PROTECTED_INTENT);
    setSetupBusy(false);
    setAffirmedWorkingTarget(null);
    normalRunStateRef.current = {
      choice: "working_target",
      verifyGoal: DEFAULT_VERIFY_GOAL,
      protectedIntent: DEFAULT_PROTECTED_INTENT,
    };
  }, [documentId, storage, storeId]);

  useEffect(() => {
    if (armedRecheck !== null) return;
    normalRunStateRef.current = {
      choice,
      verifyGoal,
      protectedIntent,
    };
  }, [armedRecheck, choice, protectedIntent, verifyGoal]);

  useEffect(() => {
    if (armedRecheck === null) return;
    setExpanded("verify");
    saveDockPanel(storage, storeId, documentId, "verify");
    setChoice(
      armedRecheck.status === "user_action_required"
        ? "working_target"
        : armedRecheck.originalActionTarget.source ?? "working_target",
    );
    setVerifyGoal(armedRecheck.userGoal);
    setProtectedIntent(armedRecheck.protectedIntent);
    setAffirmedWorkingTarget(null);
    setError(null);
    setMessage(
      armedRecheck.status === "user_action_required"
        ? "Bound recheck ready. Set and affirm Working on, then run Verify."
        : "Bound recheck ready with its exact original target. Review it, then run Verify.",
    );
  }, [
    armedRecheck,
    documentId,
    storage,
    storeId,
  ]);

  const setPanel = (panel: DockPanel): void => {
    setExpanded((current) => {
      const next = current === panel ? null : panel;
      saveDockPanel(storage, storeId, documentId, next);
      return next;
    });
  };

  const configuration = data?.verificationConfiguration;
  const capability = data?.verifyCapability ?? verifyCapability ?? null;
  const matchingReviewData =
    reviewStatus === "ready" &&
    data !== null &&
    data.documentId === documentId &&
    configuration?.documentId === documentId;
  const plan = matchingReviewData
    ? configuration.executionPlan
    : null;
  const authoritativePlanReady =
    plan?.authoritative === true &&
    plan.schema === VERIFY_EXECUTION_PLAN_SCHEMA;
  const normalExecution =
    execution?.snapshot === null || execution?.snapshot === undefined
      ? null
      : {
          providerId: execution.snapshot.selection.providerId,
          modelId: execution.snapshot.selection.modelId,
          providerLabel: execution.snapshot.selection.providerLabel,
          modelLabel: execution.snapshot.selection.modelLabel,
        };
  const selectedExecution: CoworkVerifyExecutionSelection | null =
    armedRecheck?.execution ?? normalExecution;
  const selectedProvider = execution?.snapshot?.providers.find(
    (provider) => provider.id === selectedExecution?.providerId,
  );
  const selectedModel = selectedProvider?.models.find(
    (model) => model.id === selectedExecution?.modelId,
  );
  const selectedExecutionAvailable =
    selectedExecution !== null &&
    execution?.status === "ready" &&
    execution.selecting === false &&
    selectedProvider?.available === true &&
    selectedModel?.available === true;
  const setupSummary = useMemo(() => {
    if (configuration === undefined) return verifySetup ?? null;
    return {
      activeCount: configuration.criteria.filter(
        (criterion) => criterion.operationalState === "active",
      ).length,
      unavailableCount: configuration.criteria.filter(
        (criterion) =>
          criterion.operationalState === "unavailable" ||
          criterion.operationalState === "blocked_required_check",
      ).length,
    };
  }, [configuration, verifySetup]);
  const latestRun =
    data?.evaluationRuns === undefined || data.evaluationRuns.length === 0
      ? undefined
      : latestFirst(data.evaluationRuns)[0];
  const contractSupported =
    capability?.enabled === true && capability.contractVersion === 1;
  const chosenTargetUnavailable =
    armedRecheck === null &&
    ((choice === "working_target" &&
      targetState.workingTarget.kind === "unresolved") ||
      (choice === "current_selection" && targetState.selection === null) ||
      (choice === "current_section" && targetState.currentSection === null));
  const workingTargetKey =
    targetState.workingTarget.kind === "text_range" &&
    targetState.workingTarget.range !== null
      ? `${targetState.workingTarget.range.from.toString()}:${targetState.workingTarget.range.to.toString()}:${targetState.workingTarget.label}`
      : null;
  const legacyRecheck = armedRecheck?.status === "user_action_required";
  const durableRecheck = armedRecheck?.status === "pending_capture";
  const durableRecheckTargetAvailable =
    durableRecheck &&
    armedRecheck.originalActionTarget.source !== null &&
    (armedRecheck.originalActionTarget.kind === "document" ||
      (armedRecheck.originalActionTarget.targetReference !== null &&
        controller?.captureReference !== undefined));
  const boundRecheckTargetUnavailable =
    armedRecheck !== null &&
    ((legacyRecheck &&
      (onAffirmRecheckTarget === undefined ||
        workingTargetKey === null ||
        affirmedWorkingTarget?.workingTargetKey !== workingTargetKey)) ||
      (durableRecheck && !durableRecheckTargetAvailable) ||
      (!legacyRecheck && !durableRecheck));
  const runDisabled =
    controller === null ||
    targetState.phase !== "ready" ||
    readOnly ||
    onRunVerify === undefined ||
    !contractSupported ||
    capability?.canRun !== true ||
    setupSummary?.activeCount === 0 ||
    !authoritativePlanReady ||
    !selectedExecutionAvailable ||
    verifyGoal.trim().length === 0 ||
    protectedIntent.trim().length === 0 ||
    setupBusy ||
    busy ||
    chosenTargetUnavailable ||
    boundRecheckTargetUnavailable;

  const workingLabel =
    targetState.workingTarget.kind === "unresolved"
      ? `${targetState.workingTarget.label} · needs attention`
      : `${targetState.workingTarget.label} · ${targetState.workingTarget.wordCount.toLocaleString()} words`;

  const clearBoundRecheck = (): void => {
    const normal = normalRunStateRef.current;
    setChoice(normal.choice);
    setVerifyGoal(normal.verifyGoal);
    setProtectedIntent(normal.protectedIntent);
    setAffirmedWorkingTarget(null);
    onClearArmedRecheck?.();
  };

  const captureVerifyTarget = async () => {
    if (controller === null) {
      throw new Error("Co-work Verify is waiting for the editor.");
    }
    if (armedRecheck === null) return controller.capture(choice);
    if (armedRecheck.status === "user_action_required") {
      return controller.capture("working_target");
    }
    const source = armedRecheck.originalActionTarget.source;
    if (source === null) {
      throw new Error("The original Verify target source is unavailable.");
    }
    if (armedRecheck.originalActionTarget.kind === "document") {
      return controller.captureReference === undefined
        ? controller.capture(source)
        : controller.captureReference(source, null);
    }
    const reference = armedRecheck.originalActionTarget.targetReference;
    if (reference === null || controller.captureReference === undefined) {
      throw new Error(
        "The exact original Verify target cannot be resolved by this editor.",
      );
    }
    return controller.captureReference(source, reference);
  };

  const exactWorkingTarget = (
    captured: CoworkCapturedActionSnapshot,
  ) => {
    const reference = captured.target.targetReference;
    if (
      captured.target.source !== "working_target" ||
      captured.target.selector.kind !== "text_quote" ||
      reference === undefined ||
      reference.granularity !== "character"
    ) {
      throw new Error(
        "Working on must identify one exact text passage before it can be affirmed.",
      );
    }
    return reference;
  };

  const affirmWorkingTarget = (): void => {
    const recheck = armedRecheck;
    if (
      controller === null ||
      workingTargetKey === null ||
      recheck?.status !== "user_action_required" ||
      onAffirmRecheckTarget === undefined ||
      busy
    ) {
      return;
    }
    const affirmedKey = workingTargetKey;
    setBusy(true);
    setError(null);
    setMessage("Capturing the exact Working on passage for affirmation…");
    void controller
      .capture("working_target")
      .then(async (captured) => {
        const reference = exactWorkingTarget(captured);
        const targetReferenceSha256 =
          await coworkTargetReferenceIdentitySha256(reference);
        const receipt = await onAffirmRecheckTarget(captured, {
          intentId: recheck.intentId,
          sourceRunId: recheck.sourceRunId,
          pendingProposalIds: recheck.pendingProposalIds,
          userGoal: recheck.userGoal,
          protectedIntent: recheck.protectedIntent,
        });
        if (
          receipt.recheckIntentId !== recheck.intentId ||
          receipt.sourceRunId !== recheck.sourceRunId ||
          receipt.affirmedCaptureId !== captured.captureId ||
          receipt.targetReferenceSha256 !== targetReferenceSha256 ||
          receipt.targetTextSha256 !== captured.target.targetTextSha256
        ) {
          throw new Error(
            "Co-work returned a mismatched target-affirmation receipt.",
          );
        }
        setAffirmedWorkingTarget({
          workingTargetKey: affirmedKey,
          receipt,
        });
        setMessage("Working on affirmed for this bound recheck.");
      })
      .catch((cause: unknown) => {
        setAffirmedWorkingTarget(null);
        setMessage(null);
        setError(actionErrorMessage(cause));
      })
      .finally(() => setBusy(false));
  };

  const confirmationForRun = async (
    captured: CoworkCapturedActionSnapshot,
  ): Promise<CoworkVerifyRecheckTargetConfirmation> => {
    if (affirmedWorkingTarget === null) {
      throw new Error(
        "Affirm the exact Working on passage before starting this bound recheck.",
      );
    }
    const reference = exactWorkingTarget(captured);
    const targetReferenceSha256 =
      await coworkTargetReferenceIdentitySha256(reference);
    if (
      targetReferenceSha256 !==
        affirmedWorkingTarget.receipt.targetReferenceSha256 ||
      captured.target.targetTextSha256 !==
        affirmedWorkingTarget.receipt.targetTextSha256
    ) {
      setAffirmedWorkingTarget(null);
      throw new Error(
        "Working on changed after it was affirmed. Review and affirm the exact passage again.",
      );
    }
    return {
      schema: "work-buddy.cowork-recheck-target-confirmation/v1",
      method: "user_affirmed_working_target",
      affirmedCaptureId: affirmedWorkingTarget.receipt.affirmedCaptureId,
      affirmedActionSnapshotId:
        affirmedWorkingTarget.receipt.affirmedActionSnapshotId,
      runCaptureId: captured.captureId,
      targetReferenceSha256,
      targetTextSha256: captured.target.targetTextSha256,
    };
  };

  const runVerify = (): void => {
    if (
      controller === null ||
      onRunVerify === undefined ||
      selectedExecution === null ||
      runDisabled
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    setMessage("Capturing an exact document version…");
    void captureVerifyTarget()
      .then(async (captured) => {
        const targetConfirmation = legacyRecheck
          ? await confirmationForRun(captured)
          : undefined;
        return onRunVerify(captured, {
          userGoal:
            armedRecheck === null ? verifyGoal.trim() : verifyGoal,
          protectedIntent:
            armedRecheck === null
              ? protectedIntent.trim()
              : protectedIntent,
          execution: selectedExecution,
          ...(armedRecheck === null
            ? {}
            : {
                recheck: {
                  intentId: armedRecheck.intentId,
                  sourceRunId: armedRecheck.sourceRunId,
                  pendingProposalIds: armedRecheck.pendingProposalIds,
                  ...(targetConfirmation === undefined
                    ? {}
                    : { targetConfirmation }),
                },
              }),
        });
      })
      .then(
        () => {
          setMessage(
            armedRecheck === null
              ? "Co-work Verify started on the captured version."
              : "The bound recheck started on the captured passage.",
          );
          if (armedRecheck !== null) clearBoundRecheck();
        },
        (runError: unknown) => {
          setMessage(null);
          setError(actionErrorMessage(runError));
        },
      )
      .finally(() => setBusy(false));
  };

  const selectedExecutionLabel =
    selectedExecution === null
      ? "execution selection unattested"
      : `${selectedExecution.providerLabel} · ${selectedExecution.modelLabel}`;
  const selectedCostControl =
    plan === null || selectedExecution === null
      ? null
      : plan.coordination.providerCostControls.find(
          (control) => control.providerId === selectedExecution.providerId,
        ) ??
        (plan.coordination.costControl?.providerId ===
        selectedExecution.providerId
          ? plan.coordination.costControl
          : null);
  const costCeiling =
    typeof selectedCostControl?.ceilingUsdPerWorkerSession === "number" &&
    Number.isFinite(selectedCostControl.ceilingUsdPerWorkerSession)
      ? `$${selectedCostControl.ceilingUsdPerWorkerSession.toFixed(2)} per worker session`
      : "ceiling unattested";
  const costControlLabel =
    selectedCostControl === null
      ? "Cost control · unattested"
      : `Cost control · ${planValue(selectedCostControl.enforcementClass)} · ${costCeiling}`;
  const conditionalRoles =
    plan === null || plan.coordination.workerSessions.conditionalRoles.length === 0
      ? "none attested"
      : plan.coordination.workerSessions.conditionalRoles
          .map(planValue)
          .join(", ");
  const disclosureHelp: HelpContent = {
    summary:
      "Verify shows the execution boundary attested for this document and run selection.",
    details:
      !authoritativePlanReady || plan === null
        ? "No matching authoritative execution disclosure is currently available, so Run Verify stays disabled."
        : `Plan schema=${rawPlanValue(plan.schema)}; authoritative=${rawPlanValue(plan.authoritative)}. Checker mechanism=${rawPlanValue(plan.checker.mechanism)}, model_call=${rawPlanValue(plan.checker.modelCall)}, external_egress=${rawPlanValue(plan.checker.externalEgress)}. Coordinator execution_class=${rawPlanValue(plan.coordination.executionClass)}, selection_mode=${rawPlanValue(plan.coordination.selection.mode)}, external_egress=${rawPlanValue(plan.coordination.externalEgress)}. Conditional roles=${plan.coordination.workerSessions.conditionalRoles.map(rawPlanValue).join(", ") || "unattested"}. Cost basis=${rawPlanValue(selectedCostControl?.basis)}.`,
  };
  const goalHelp: HelpContent = {
    summary: "State the outcome this Verify run should evaluate or improve.",
    details:
      "The coordinator receives this exact goal with the frozen document and active criteria.",
  };
  const protectedIntentHelp: HelpContent = {
    summary: "Name meaning, voice, constraints, or decisions that must survive.",
    details:
      "Verify carries this exact protected intent into coordination and any requested revision.",
  };
  const cothinkHelp: HelpContent = {
    summary: "Co-think is for non-evidential alternative perspectives.",
    details:
      "It remains distinct from Verify findings and cannot become a correction or verified claim merely by being surfaced.",
  };

  return (
    <section className="wb-cowork-action-dock" aria-label="Co-work tools">
      <div className="wb-cowork-action-dock__headers">
        <button
          type="button"
          id="wb-cowork-dock-trigger-verify"
          className="wb-cowork-action-dock__trigger"
          aria-label="Verify"
          aria-expanded={expanded === "verify"}
          aria-controls="wb-cowork-dock-panel-verify"
          onClick={() => setPanel("verify")}
        >
          <span>Verify</span>
          <span className="wb-cowork-action-dock__trigger-summary">
            {setupSummary === null
              ? "Setup loading"
              : `${setupSummary.activeCount.toLocaleString()} active`}
            {latestRun === undefined ? "" : ` · ${RUN_LABEL[latestRun.status]}`}
          </span>
        </button>
        <button
          type="button"
          id="wb-cowork-dock-trigger-cothink"
          className="wb-cowork-action-dock__trigger"
          aria-label="Co-think"
          aria-expanded={expanded === "cothink"}
          aria-controls="wb-cowork-dock-panel-cothink"
          onClick={() => setPanel("cothink")}
        >
          <span>Co-think</span>
          <span className="wb-cowork-action-dock__trigger-summary">
            Planned
          </span>
        </button>
      </div>

      <div
        id="wb-cowork-dock-panel-verify"
        className="wb-cowork-action-dock__panel"
        role="region"
        aria-labelledby="wb-cowork-dock-trigger-verify"
        hidden={expanded !== "verify"}
        inert={expanded !== "verify" ? true : undefined}
      >
          <div className="wb-cowork-action-dock__run">
            <div className="wb-cowork-action-dock__run-copy">
              <strong>Co-work Verify</strong>
              <ul className="wb-cowork-action-dock__disclosure">
                {authoritativePlanReady && plan !== null ? (
                  <>
                    <li>
                      Checker · {planValue(plan.checker.contentBoundary)} ·{" "}
                      {planValue(plan.checker.executionClass)} ·{" "}
                      {planToggle(
                        plan.checker.externalEgress,
                        "external egress",
                        "no external egress",
                      )}
                    </li>
                    <li>
                      Coordinator · {selectedExecutionLabel} ·{" "}
                      {planValue(plan.coordination.executionClass)} ·{" "}
                      {planValue(plan.coordination.contentBoundary)}
                    </li>
                    <li>
                      Sessions ·{" "}
                      {planValue(plan.coordination.workerSessions.initial)}{" "}
                      initial ·{" "}
                      {planValue(plan.coordination.workerSessions.maximum)}{" "}
                      maximum · {conditionalRoles} when needed
                    </li>
                    <li>
                      {costControlLabel} ·{" "}
                      {planToggle(
                        plan.coordination.fallback.providerModelFallback,
                        "provider/model fallback allowed",
                        "no provider/model fallback",
                      )}{" "}
                      ·{" "}
                      {planValue(plan.coordination.fallback.failureMode)}
                    </li>
                  </>
                ) : (
                  <li>
                    Execution disclosure · unknown/unattested for this
                    document
                  </li>
                )}
              </ul>
              <HelpTarget content={disclosureHelp} placement="top start" focusable>
                <span className="wb-cowork-action-dock__help">
                  How checks, coordination, and cost work
                </span>
              </HelpTarget>
            </div>
            <div className="wb-cowork-action-dock__run-actions">
              {armedRecheck !== null ? (
                <p
                  className="wb-cowork-action-dock__execution"
                  aria-label="Bound recheck execution"
                >
                  Original run model · {selectedExecutionLabel}
                </p>
              ) : execution === undefined ? null : (
                <ChatExecutionPicker
                  control={execution}
                  disabled={busy || setupBusy}
                  readOnly={readOnly}
                  className="wb-cowork-action-dock__execution"
                />
              )}
              <Button
                variant="primary"
                size="small"
                disabled={runDisabled}
                title={
                  readOnly
                    ? "Read-only sessions cannot start Co-work Verify"
                    : !matchingReviewData
                      ? "Waiting for this document’s authoritative Verify setup"
                      : !authoritativePlanReady
                        ? "This document has no supported authoritative execution disclosure"
                        : !selectedExecutionAvailable
                          ? "The exact provider and model are unavailable"
                          : setupBusy
                            ? "Wait for the Verify setup change to finish"
                            : boundRecheckTargetUnavailable
                              ? legacyRecheck
                                ? "Affirm the exact Working on passage before starting this bound recheck"
                                : "The exact original target is unavailable, so this bound recheck cannot start"
                    : setupSummary?.activeCount === 0
                      ? "Turn on at least one available criterion in Verify setup"
                      : !contractSupported || capability?.canRun !== true
                        ? capability?.disabledReason ??
                          "This Co-work Verify contract is unavailable"
                        : onRunVerify === undefined
                          ? "Co-work Verify is unavailable for this document"
                          : undefined
                }
                onClick={runVerify}
              >
                {busy ? "Capturing…" : "Run Verify"}
              </Button>
            </div>
          </div>

          {armedRecheck !== null ? (
            <div className="wb-cowork-action-dock__notice" role="status">
              <p>
                <strong>Bound recheck.</strong>{" "}
                {legacyRecheck
                  ? "Set Working on to the exact earlier passage, then affirm it below."
                  : "The exact durable target from the earlier run is bound below."}{" "}
                This run keeps the original model, goal, and correction
                lineage.
              </p>
              <p>
                Earlier target:{" "}
                {armedRecheck.originalActionTarget.label ??
                  "legacy passage without a durable label"}
                {legacyRecheck ? `. Current Working on: ${workingLabel}.` : "."}
              </p>
              {legacyRecheck ? (
                <Button
                  size="small"
                  variant="secondary"
                  disabled={
                    busy ||
                    workingTargetKey === null ||
                    onAffirmRecheckTarget === undefined
                  }
                  onClick={affirmWorkingTarget}
                >
                  {workingTargetKey !== null &&
                  affirmedWorkingTarget?.workingTargetKey === workingTargetKey
                    ? "Working on affirmed"
                    : "Use this Working on passage"}
                </Button>
              ) : null}
              <Button
                size="small"
                variant="secondary"
                disabled={busy}
                onClick={() => {
                  setMessage(null);
                  setError(null);
                  clearBoundRecheck();
                }}
              >
                Cancel recheck
              </Button>
            </div>
          ) : null}

          <div className="wb-cowork-action-dock__intent">
            {durableRecheck ? (
              <div
                className="wb-cowork-action-dock__bound-target"
                role="group"
                aria-label="Verify target"
              >
                <span>Verify target</span>
                <strong>
                  Original target ·{" "}
                  {armedRecheck.originalActionTarget.label ??
                    (armedRecheck.originalActionTarget.kind === "document"
                      ? "Whole document"
                      : "Exact earlier passage")}
                </strong>
              </div>
            ) : (
              <SelectField<CoworkActionTargetChoice>
                label="Verify target"
                value={choice}
                compact
                options={[
                  {
                    value: "working_target",
                    label: "Working on",
                    description: workingLabel,
                    disabled: targetState.workingTarget.kind === "unresolved",
                  },
                  {
                    value: "current_selection",
                    label: "Current selection · one run",
                    description:
                      targetState.selection === null
                        ? "Select document text first"
                        : `${targetState.selection.label} · ${targetState.selection.wordCount.toLocaleString()} words`,
                    disabled: targetState.selection === null,
                  },
                  {
                    value: "current_section",
                    label: "Current section · one run",
                    description:
                      targetState.currentSection === null
                        ? "No section is available"
                        : `${targetState.currentSection.label} · ${targetState.currentSection.wordCount.toLocaleString()} words`,
                    disabled: targetState.currentSection === null,
                  },
                  {
                    value: "whole_document",
                    label: "Whole document · one run",
                  },
                ]}
                disabled={
                  controller === null || busy || armedRecheck !== null
                }
                onChange={setChoice}
              />
            )}
            <TextAreaField
              label="What should Verify accomplish?"
              value={verifyGoal}
              disabled={busy || armedRecheck !== null}
              rows={2}
              help={goalHelp}
              onChange={setVerifyGoal}
            />
            <TextAreaField
              label="What must it preserve?"
              value={protectedIntent}
              disabled={busy || armedRecheck !== null}
              rows={2}
              help={protectedIntentHelp}
              onChange={setProtectedIntent}
            />
          </div>

          {reviewStatus === "error" ? (
            <div className="wb-cowork-action-dock__notice" role="alert">
              <p>{reviewError ?? "Verify setup could not load."}</p>
              <Button size="small" variant="secondary" onClick={reload}>
                Retry
              </Button>
            </div>
          ) : reviewStatus === "loading" ||
            data === null ||
            !matchingReviewData ? (
            <p className="wb-cowork-action-dock__notice" role="status">
              Loading this document’s Verify setup…
            </p>
          ) : (
            <>
              <VerifySetupCard
                capability={data.verifyCapability}
                configuration={data.verificationConfiguration}
                onBusyChange={setSetupBusy}
                onSetEnabled={
                  reviewProvider.setVerifyCriterionEnabled === undefined
                    ? undefined
                    : async (criterionKey, enabled, expectedActivationId) => {
                        await reviewProvider.setVerifyCriterionEnabled?.(
                          criterionKey,
                          enabled,
                          expectedActivationId,
                        );
                      }
                }
                onCreateDraft={
                  reviewProvider.createVerifyCriterionDraft === undefined
                    ? undefined
                    : async (draft) => {
                        await reviewProvider.createVerifyCriterionDraft?.(draft);
                      }
                }
              />
              <VerifyRunHistory
                runs={data.evaluationRuns}
                onInspectRun={reviewProvider.inspectVerifyRun?.bind(
                  reviewProvider,
                )}
              />
            </>
          )}

          <p
            className="wb-cowork-action-dock__status"
            role={error === null ? "status" : "alert"}
            aria-live={error === null ? "polite" : "assertive"}
          >
            {error ?? message ?? ""}
          </p>
      </div>

      <div
        id="wb-cowork-dock-panel-cothink"
        className="wb-cowork-action-dock__panel wb-cowork-action-dock__panel--cothink"
        role="region"
        aria-labelledby="wb-cowork-dock-trigger-cothink"
        hidden={expanded !== "cothink"}
        inert={expanded !== "cothink" ? true : undefined}
      >
          <p>
            Co-think is reserved here as a sibling workspace for alternative
            perspectives. It does not run from this shell yet.
          </p>
          <HelpTarget content={cothinkHelp} placement="top start" focusable>
            <span className="wb-cowork-action-dock__help">
              How Co-think differs from Verify
            </span>
          </HelpTarget>
      </div>
    </section>
  );
}
