import {
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";

import { HelpTarget, type HelpContent } from "../../../dashboard/help";
import { Button } from "../../../ui";
import type { ChatExecutionControl } from "../../../widget-library/chat";
import {
  useReviewData,
  type CoworkVerifyCapability,
  type ReviewRailProvider,
} from "../rail";
import {
  VerifyCheckControl,
  type VerifyCheckPage,
} from "../verify";
import type { VerificationRecheckIntent } from "../rail/contracts";
import type {
  CoworkActionSnapshotController,
  CoworkActionSnapshotControllerState,
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
  "Evaluate the current Working on target with the selected verification checks.";
const DEFAULT_PROTECTED_INTENT =
  "Preserve the author's intended meaning, voice, and constraints.";
const VERIFY_EXECUTION_PLAN_SCHEMA =
  "work-buddy.cowork-verify-execution-disclosure/v1";

const VERIFY_DOCK_HELP: HelpContent = {
  summary: "Choose checks and run them against Working on.",
  details:
    "Verify captures the current Working on passage, executes the selected checks, and sends only coordinated findings or proposals to Review.",
};

const COTHINK_DOCK_HELP: HelpContent = {
  summary: "Challenge or explore the work from another angle.",
  details:
    "Co-think supports provocations and Socratic questions; Verify evaluates selected checks and may propose corrections.",
};

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

const subscribeFallback = (): (() => void) => () => undefined;
const getFallback = (): CoworkActionSnapshotControllerState => FALLBACK_STATE;

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
  const [verifyPage, setVerifyPage] =
    useState<VerifyCheckPage>("select");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [setupBusy, setSetupBusy] = useState(false);
  const [affirmedWorkingTarget, setAffirmedWorkingTarget] =
    useState<AffirmedWorkingTarget | null>(null);

  useEffect(() => {
    setExpanded(loadDockPanel(storage, storeId, documentId));
    setVerifyPage("select");
    setMessage(null);
    setError(null);
    setSetupBusy(false);
    setAffirmedWorkingTarget(null);
  }, [documentId, storage, storeId]);

  useEffect(() => {
    if (armedRecheck === null) return;
    setExpanded("verify");
    saveDockPanel(storage, storeId, documentId, "verify");
    setVerifyPage("select");
    setAffirmedWorkingTarget(null);
    setError(null);
    setMessage(
      armedRecheck.status === "user_action_required"
        ? "Recheck ready. Set Working on to the earlier passage, then confirm it."
        : "Recheck ready with its original passage.",
    );
  }, [armedRecheck, documentId, storage, storeId]);

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
    if (configuration === undefined) {
      return verifySetup === null || verifySetup === undefined
        ? null
        : {
            selectedCount: verifySetup.activeCount,
            unavailableSelectedCount: 0,
          };
    }
    return {
      selectedCount: configuration.criteria.filter(
        (criterion) => criterion.enabled,
      ).length,
      unavailableSelectedCount: configuration.criteria.filter(
        (criterion) =>
          criterion.enabled && criterion.operationalState !== "active",
      ).length,
    };
  }, [configuration, verifySetup]);
  const contractSupported =
    capability?.enabled === true && capability.contractVersion === 1;
  const workingTargetUnavailable =
    armedRecheck === null &&
    targetState.workingTarget.kind === "unresolved";
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
    setupSummary?.selectedCount === 0 ||
    (setupSummary?.unavailableSelectedCount ?? 0) > 0 ||
    !authoritativePlanReady ||
    !selectedExecutionAvailable ||
    setupBusy ||
    busy ||
    workingTargetUnavailable ||
    boundRecheckTargetUnavailable;

  const clearBoundRecheck = (): void => {
    setAffirmedWorkingTarget(null);
    onClearArmedRecheck?.();
  };

  const captureVerifyTarget = async () => {
    if (controller === null) {
      throw new Error("Co-work Verify is waiting for the editor.");
    }
    if (armedRecheck === null) return controller.capture("working_target");
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
            armedRecheck?.userGoal ?? DEFAULT_VERIFY_GOAL,
          protectedIntent:
            armedRecheck?.protectedIntent ?? DEFAULT_PROTECTED_INTENT,
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
              ? "Verify started."
              : "Recheck started.",
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

  const runUnavailableReason = readOnly
    ? "Read-only sessions cannot start Verify"
    : !matchingReviewData
      ? "Loading checks"
      : !authoritativePlanReady
        ? "Verify is not ready for this document"
        : !selectedExecutionAvailable
          ? "Verify’s execution model is unavailable"
          : setupBusy
            ? "Wait for the selected checks to finish updating"
            : boundRecheckTargetUnavailable
              ? legacyRecheck
                ? "Confirm the Working on passage before running this recheck"
                : "The original recheck passage is unavailable"
              : workingTargetUnavailable
                ? "Set Working on before running Verify"
                : (setupSummary?.unavailableSelectedCount ?? 0) > 0
                  ? "Turn off checks that need setup"
                  : setupSummary?.selectedCount === 0
                  ? "Select at least one check"
                  : !contractSupported || capability?.canRun !== true
                    ? capability?.disabledReason ?? "Verify is unavailable"
                    : onRunVerify === undefined
                      ? "Verify is unavailable"
                      : undefined;
  const readinessMessage =
    !readOnly &&
    matchingReviewData &&
    (setupSummary?.unavailableSelectedCount ?? 0) > 0
      ? "Turn off checks that need setup."
      : !readOnly &&
          matchingReviewData &&
          execution !== undefined &&
          execution?.status !== "loading" &&
          !selectedExecutionAvailable
        ? "Verify needs an available account model."
        : null;

  return (
    <section className="wb-cowork-action-dock" aria-label="Co-work tools">
      <div className="wb-cowork-action-dock__headers">
        <HelpTarget content={VERIFY_DOCK_HELP} placement="top start">
          <button
            type="button"
            id="wb-cowork-dock-trigger-verify"
            className="wb-cowork-action-dock__trigger"
            aria-label="Verify"
            aria-describedby="wb-cowork-dock-summary-verify"
            aria-expanded={expanded === "verify"}
            aria-controls="wb-cowork-dock-panel-verify"
            onClick={() => setPanel("verify")}
          >
            <span>Verify</span>
            <span
              id="wb-cowork-dock-summary-verify"
              className="wb-cowork-action-dock__trigger-summary"
            >
              {setupSummary === null
                ? "Loading…"
                : verifyPage === "add"
                  ? "Adding check"
                  : `${setupSummary.selectedCount.toLocaleString()} selected`}
            </span>
          </button>
        </HelpTarget>
        <HelpTarget content={COTHINK_DOCK_HELP} placement="top">
          <button
            type="button"
            id="wb-cowork-dock-trigger-cothink"
            className="wb-cowork-action-dock__trigger"
            aria-label="Co-think"
            aria-describedby="wb-cowork-dock-summary-cothink"
            aria-expanded={expanded === "cothink"}
            aria-controls="wb-cowork-dock-panel-cothink"
            onClick={() => setPanel("cothink")}
          >
            <span>Co-think</span>
            <span
              id="wb-cowork-dock-summary-cothink"
              className="wb-cowork-action-dock__trigger-summary"
            >
              Planned
            </span>
          </button>
        </HelpTarget>
      </div>

      <div
        id="wb-cowork-dock-panel-verify"
        className="wb-cowork-action-dock__panel"
        role="region"
        aria-labelledby="wb-cowork-dock-trigger-verify"
        hidden={expanded !== "verify"}
        inert={expanded !== "verify" ? true : undefined}
      >
        {armedRecheck !== null ? (
          <div className="wb-cowork-action-dock__notice" role="status">
            <p>
              <strong>Recheck.</strong>{" "}
              {legacyRecheck
                ? "Set Working on to the earlier passage, then use that passage."
                : `Original passage: ${
                    armedRecheck.originalActionTarget.label ??
                    "the earlier Verify target"
                  }.`}
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
                  ? "Passage selected"
                  : "Use this passage"}
              </Button>
            ) : null}
            <Button
              size="small"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setMessage(null);
                setError(null);
                clearBoundRecheck();
              }}
            >
              Cancel
            </Button>
          </div>
        ) : null}

        {reviewStatus === "error" ? (
          <div className="wb-cowork-action-dock__notice" role="alert">
            <p>{reviewError ?? "Checks could not load."}</p>
            <Button size="small" variant="secondary" onClick={reload}>
              Retry
            </Button>
          </div>
        ) : reviewStatus === "loading" ||
          data === null ||
          !matchingReviewData ? (
          <p className="wb-cowork-action-dock__notice" role="status">
            Loading checks…
          </p>
        ) : (
          <div
            className="wb-cowork-action-dock__verify-page"
            data-page={verifyPage}
          >
            <VerifyCheckControl
              capability={data.verifyCapability}
              configuration={data.verificationConfiguration}
              page={verifyPage}
              onPageChange={setVerifyPage}
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
              onCreateCheck={
                reviewProvider.createVerifyCheck === undefined
                  ? undefined
                  : async (check) => {
                      await reviewProvider.createVerifyCheck?.(check);
                    }
              }
            />
            {verifyPage === "select" ? (
              <Button
                className="wb-cowork-action-dock__run-button"
                variant="primary"
                size="small"
                disabled={runDisabled}
                title={runUnavailableReason}
                aria-describedby="wb-cowork-verify-status"
                onClick={runVerify}
              >
                {busy ? "Starting…" : "Run Verify"}
              </Button>
            ) : null}
          </div>
        )}

        <p
          id="wb-cowork-verify-status"
          className="wb-cowork-action-dock__status"
          role={error === null ? "status" : "alert"}
          aria-live={error === null ? "polite" : "assertive"}
        >
          {error ?? message ?? readinessMessage ?? ""}
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
        <p className="wb-cowork-action-dock__planned">Planned</p>
      </div>
    </section>
  );
}
