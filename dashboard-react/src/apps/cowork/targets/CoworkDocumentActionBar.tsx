import {
  useEffect,
  useState,
  useSyncExternalStore,
} from "react";

import { Button, SelectField } from "../../../ui";
import type { CoworkVerifyCapability } from "../rail";
import type {
  CoworkActionSnapshotController,
  CoworkActionSnapshotControllerState,
  CoworkActionTargetChoice,
  CoworkInvitePerspectiveHandler,
  CoworkRunVerifyHandler,
} from "./contracts";
import "./styles.css";

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

const DEFAULT_VERIFY_GOAL =
  "Check this target against the active verification criteria.";
const DEFAULT_PROTECTED_INTENT =
  "Preserve the author's intended meaning, voice, and constraints.";

const errorMessage = (error: unknown): string =>
  error instanceof Error
    ? error.message
    : "Co-work Verify could not capture this document version.";

/**
 * Keyed, live-session chrome. The existing lifecycle document bar stays
 * outside the editor owner; this bar receives only the narrow target/capture
 * controller lifted by the bridge.
 */
export function CoworkDocumentActionBar({
  controller,
  readOnly = false,
  onRunVerify,
  onInvitePerspective,
  verifySetup,
  verifyCapability,
  executionLabel,
}: {
  readonly controller: CoworkActionSnapshotController | null;
  readonly readOnly?: boolean;
  readonly onRunVerify?: CoworkRunVerifyHandler;
  readonly onInvitePerspective?: CoworkInvitePerspectiveHandler;
  readonly verifySetup?: {
    readonly activeCount: number;
    readonly unavailableCount: number;
    readonly costCeilingUsdPerWorker?: number | null;
    readonly baseWorkerCalls?: number | null;
    readonly maximumWorkerCalls?: number | null;
  } | null;
  readonly verifyCapability?: CoworkVerifyCapability | null;
  readonly executionLabel?: string;
}) {
  const state = useSyncExternalStore(
    controller?.subscribe ?? subscribeFallback,
    controller?.getSnapshot ?? getFallback,
    getFallback,
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

  useEffect(() => {
    setMessage(null);
    setError(null);
  }, [controller]);

  const selectionReady =
    state.phase === "ready" && state.selection !== null;
  const chosenTargetUnavailable =
    (choice === "working_target" &&
      state.workingTarget.kind === "unresolved") ||
    (choice === "current_selection" && state.selection === null) ||
    (choice === "custom_range" && state.customRange == null) ||
    (choice === "current_section" && state.currentSection === null);
  const contractSupported =
    verifyCapability?.enabled === true &&
    verifyCapability.contractVersion === 1;
  const runDisabled =
    controller === null ||
    state.phase !== "ready" ||
    readOnly ||
    onRunVerify === undefined ||
    !contractSupported ||
    !verifyCapability.canRun ||
    verifySetup?.activeCount === 0 ||
    verifyGoal.trim().length === 0 ||
    protectedIntent.trim().length === 0 ||
    busy ||
    chosenTargetUnavailable;
  const cothinkDisabled =
    controller === null ||
    state.phase !== "ready" ||
    readOnly ||
    onInvitePerspective === undefined ||
    !contractSupported ||
    !verifyCapability.canCothink ||
    busy ||
    chosenTargetUnavailable;

  const workOnSelection = (): void => {
    if (controller === null) return;
    setError(null);
    setMessage(null);
    try {
      controller.setWorkingTargetFromSelection();
      setChoice("working_target");
      setMessage("Document target updated.");
    } catch (targetError) {
      setError(errorMessage(targetError));
    }
  };

  const clearTarget = (): void => {
    if (controller === null) return;
    setError(null);
    controller.clearWorkingTarget();
    setChoice("whole_document");
    setMessage("Working on the whole document.");
  };

  const setRangeStart = (): void => {
    if (controller?.setCustomRangeStartHere === undefined) return;
    setError(null);
    try {
      controller.setCustomRangeStartHere();
      setMessage(
        "Custom range start set. Move the cursor, then set the range end.",
      );
    } catch (targetError) {
      setError(errorMessage(targetError));
    }
  };

  const setRangeEnd = (): void => {
    if (controller?.setCustomRangeEndHere === undefined) return;
    setError(null);
    try {
      controller.setCustomRangeEndHere();
      setChoice("custom_range");
      setMessage("Custom range is ready for this action.");
    } catch (targetError) {
      setError(errorMessage(targetError));
    }
  };

  const clearRange = (): void => {
    controller?.clearCustomRange?.();
    if (choice === "custom_range") setChoice("working_target");
    setMessage("Custom range cleared.");
    setError(null);
  };

  const runVerify = (): void => {
    if (controller === null || onRunVerify === undefined || runDisabled) return;
    setBusy(true);
    setError(null);
    setMessage("Capturing an exact document version…");
    let capture;
    try {
      capture = controller.capture(choice);
    } catch (captureError) {
      setBusy(false);
      setMessage(null);
      setError(errorMessage(captureError));
      return;
    }
    void capture
      .then((captured) =>
        onRunVerify(captured, {
          userGoal: verifyGoal.trim(),
          protectedIntent: protectedIntent.trim(),
        }),
      )
      .then(
        () => setMessage("Co-work Verify started on the captured version."),
        (runError: unknown) => {
          setMessage(null);
          setError(errorMessage(runError));
        },
      )
      .finally(() => setBusy(false));
  };

  const invitePerspective = (): void => {
    if (
      controller === null ||
      onInvitePerspective === undefined ||
      cothinkDisabled
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    setMessage("Capturing an exact document version…");
    void controller
      .capture(choice)
      .then(onInvitePerspective)
      .then(
        () => setMessage("Co-think is considering another perspective."),
        (runError: unknown) => {
          setMessage(null);
          setError(errorMessage(runError));
        },
      )
      .finally(() => setBusy(false));
  };

  const workingLabel =
    state.workingTarget.kind === "unresolved"
      ? `${state.workingTarget.label} · needs attention`
      : `${state.workingTarget.label} · ${state.workingTarget.wordCount.toLocaleString()} words`;

  return (
    <section
      className="wb-cowork-action-bar"
      aria-label="Document target, Co-work Verify, and Co-think"
    >
      <div className="wb-cowork-action-bar__working-context">
        <div className="wb-cowork-action-bar__working">
          <span className="wb-cowork-action-bar__eyebrow">Working on</span>
          <strong title={workingLabel}>{workingLabel}</strong>
          <div className="wb-cowork-action-bar__target-actions">
            <Button
              size="small"
              variant="ghost"
              disabled={!selectionReady || busy}
              onClick={workOnSelection}
            >
              Work on this
            </Button>
            {state.workingTarget.kind !== "document" ? (
              <Button
                size="small"
                variant="ghost"
                disabled={controller === null || busy}
                onClick={clearTarget}
              >
                Clear
              </Button>
            ) : null}
          </div>
        </div>
        {controller?.setCustomRangeStartHere !== undefined ? (
          <details className="wb-cowork-action-bar__range">
            <summary>
              Custom range
              {state.customRange === null || state.customRange === undefined
                ? state.customRangeStart === null ||
                  state.customRangeStart === undefined
                  ? ""
                  : " · start set"
                : ` · ${state.customRange.wordCount.toLocaleString()} words`}
            </summary>
            <p>
              Set two exact block boundaries. These accessible boundary
              controls are the keyboard equivalent of start and end range
              handles.
            </p>
            <div className="wb-cowork-action-bar__range-actions">
              <Button
                size="small"
                variant="ghost"
                disabled={busy}
                onClick={setRangeStart}
              >
                ↦ Set start here
              </Button>
              <Button
                size="small"
                variant="ghost"
                disabled={
                  busy ||
                  state.customRangeStart === null ||
                  state.customRangeStart === undefined
                }
                onClick={setRangeEnd}
              >
                ↤ Set end here
              </Button>
              {state.customRange !== null &&
              state.customRange !== undefined ? (
                <Button
                  size="small"
                  variant="ghost"
                  disabled={busy}
                  onClick={clearRange}
                >
                  Clear range
                </Button>
              ) : null}
            </div>
          </details>
        ) : null}
      </div>
      <div className="wb-cowork-action-bar__target">
        <SelectField<CoworkActionTargetChoice>
          label="Action target"
          value={choice}
          compact
          options={[
            {
              value: "working_target",
              label: "Working on",
              description: workingLabel,
              disabled: state.workingTarget.kind === "unresolved",
            },
            {
              value: "current_selection",
              label: "Current selection · one run",
              description:
                state.selection === null
                  ? "Select document text first"
                  : `${state.selection.label} · ${state.selection.wordCount.toLocaleString()} words`,
              disabled: state.selection === null,
            },
            {
              value: "custom_range",
              label: "Custom range",
              description:
                state.customRange === null ||
                state.customRange === undefined
                  ? "Set start and end boundaries first"
                  : `${state.customRange.label} · ${state.customRange.wordCount.toLocaleString()} words`,
              disabled:
                state.customRange === null ||
                state.customRange === undefined,
            },
            {
              value: "current_section",
              label: "Current section",
              description:
                state.currentSection === null
                  ? "No section is available"
                  : `${state.currentSection.label} · ${state.currentSection.wordCount.toLocaleString()} words`,
              disabled: state.currentSection === null,
            },
            {
              value: "whole_document",
              label: "Whole document",
            },
          ]}
          disabled={controller === null || busy}
          onChange={setChoice}
        />
      </div>
      <div
        className="wb-cowork-action-bar__verify"
        aria-label="Co-work Verify action"
      >
        <div className="wb-cowork-action-bar__action-copy">
          <strong className="wb-cowork-action-bar__verify-summary">
            Co-work Verify
            {verifySetup === null || verifySetup === undefined
              ? ""
              : ` · ${verifySetup.activeCount.toLocaleString()} active${
                  verifySetup.unavailableCount > 0
                    ? ` · ${verifySetup.unavailableCount.toLocaleString()} unavailable`
                    : ""
                }`}
            {executionLabel === undefined ? "" : ` · ${executionLabel}`}
          </strong>
          {executionLabel !== undefined && contractSupported ? (
            <span className="wb-cowork-action-bar__sharing">
              Shares the complete permitted frozen document with this
              account-backed model for forest-level coordination
              {verifySetup?.costCeilingUsdPerWorker == null
                ? "."
                : `: ${String(verifySetup.baseWorkerCalls ?? 1)} call normally, up to ${String(verifySetup.maximumWorkerCalls ?? 3)} at $${verifySetup.costCeilingUsdPerWorker.toFixed(2)} each when revision is requested.`}
            </span>
          ) : null}
        </div>
        <details className="wb-cowork-action-bar__intent">
          <summary>Goal and protected intent</summary>
          <label>
            What should Verify accomplish?
            <textarea
              value={verifyGoal}
              disabled={busy}
              rows={2}
              onChange={(event) => setVerifyGoal(event.currentTarget.value)}
            />
          </label>
          <label>
            What must it preserve?
            <textarea
              value={protectedIntent}
              disabled={busy}
              rows={2}
              onChange={(event) =>
                setProtectedIntent(event.currentTarget.value)
              }
            />
          </label>
        </details>
        <Button
          variant="primary"
          size="small"
          disabled={runDisabled}
          title={
            readOnly
              ? "Read-only sessions cannot start Co-work Verify"
              : verifySetup?.activeCount === 0
                ? "Turn on at least one available criterion in Verify setup"
              : verifyGoal.trim().length === 0 ||
                  protectedIntent.trim().length === 0
                ? "Add both a Verify goal and the intent it must preserve"
              : !contractSupported || !verifyCapability.canRun
                ? verifyCapability?.disabledReason ??
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
      <div
        className="wb-cowork-action-bar__cothink"
        aria-label="Co-think action"
      >
        <div className="wb-cowork-action-bar__action-copy">
          <strong className="wb-cowork-action-bar__verify-summary">
            Co-think
            {executionLabel === undefined ? "" : ` · ${executionLabel}`}
          </strong>
          <span className="wb-cowork-action-bar__sharing">
            Sends the complete permitted frozen document to this
            account-backed model for one non-evidential perspective on the
            same captured target
            {verifySetup?.costCeilingUsdPerWorker == null
              ? "."
              : `, capped at $${verifySetup.costCeilingUsdPerWorker.toFixed(2)}.`}
          </span>
        </div>
        <Button
          variant="secondary"
          size="small"
          disabled={cothinkDisabled}
          title={
            !contractSupported || !verifyCapability.canCothink
              ? verifyCapability?.disabledReason ??
                "Co-think is unavailable for this document"
            : onInvitePerspective === undefined
              ? "Co-think is unavailable for this document"
              : undefined
          }
          onClick={invitePerspective}
        >
          Invite perspective
        </Button>
      </div>
      <p
        className="wb-cowork-action-bar__status"
        role={error === null ? "status" : "alert"}
        aria-live={error === null ? "polite" : "assertive"}
      >
        {error ?? message ?? ""}
      </p>
    </section>
  );
}
