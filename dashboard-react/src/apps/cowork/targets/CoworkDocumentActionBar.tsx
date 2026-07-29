import { useState, useSyncExternalStore } from "react";

import { HelpTarget, type HelpContent } from "../../../dashboard/help";
import { Button } from "../../../ui";
import type {
  CoworkActionSnapshotController,
  CoworkActionSnapshotControllerState,
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

const errorMessage = (error: unknown): string =>
  error instanceof Error
    ? error.message
    : "Co-work could not update the document target.";

const CURSOR_RANGE_HELP: HelpContent = {
  summary: "Set the Working on range from two exact cursor positions.",
  details:
    "Place the cursor at one boundary and set the start. Move it to the other boundary and set the end. Co-work highlights the resulting text and marks both boundaries in the editor.",
};

/**
 * Compact editor-scoped target chrome. Invocation and configuration belong to
 * the bottom action dock; this bar owns only the durable Working on target and
 * its keyboard-accessible range controls.
 */
export function CoworkDocumentActionBar({
  controller,
}: {
  readonly controller: CoworkActionSnapshotController | null;
}) {
  const state = useSyncExternalStore(
    controller?.subscribe ?? subscribeFallback,
    controller?.getSnapshot ?? getFallback,
    getFallback,
  );
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const selectionReady =
    state.phase === "ready" && state.selection !== null;

  const workOnSelection = (): void => {
    if (controller === null) return;
    setError(null);
    setMessage(null);
    try {
      controller.setWorkingTargetFromSelection();
      setMessage("Working on updated.");
    } catch (targetError) {
      setError(errorMessage(targetError));
    }
  };

  const clearTarget = (): void => {
    if (controller === null) return;
    setError(null);
    controller.clearWorkingTarget();
    setMessage("Working on the whole document.");
  };

  const setRangeStart = (): void => {
    if (controller?.setWorkingTargetStartHere === undefined) return;
    setError(null);
    try {
      controller.setWorkingTargetStartHere();
      setMessage(
        "Start set. Move the cursor, then set the end.",
      );
    } catch (targetError) {
      setError(errorMessage(targetError));
    }
  };

  const setRangeEnd = (): void => {
    if (controller?.setWorkingTargetEndHere === undefined) return;
    setError(null);
    try {
      controller.setWorkingTargetEndHere();
      setMessage("Working on updated.");
    } catch (targetError) {
      setError(errorMessage(targetError));
    }
  };

  const clearRange = (): void => {
    controller?.clearWorkingTargetDraft?.();
    setMessage("Cursor range cancelled.");
    setError(null);
  };

  const workingLabel =
    state.workingTarget.kind === "unresolved"
      ? `${state.workingTarget.label} · needs attention`
      : `${state.workingTarget.label} · ${state.workingTarget.wordCount.toLocaleString()} words`;

  return (
    <section className="wb-cowork-action-bar" aria-label="Working on">
      <div className="wb-cowork-action-bar__working">
        <span className="wb-cowork-action-bar__eyebrow">Working on</span>
        <strong title={workingLabel}>{workingLabel}</strong>
        <div className="wb-cowork-action-bar__target-actions">
          <Button
            size="small"
            variant="ghost"
            disabled={!selectionReady}
            onClick={workOnSelection}
          >
            Set by selection
          </Button>
          {state.workingTarget.kind !== "document" ? (
            <Button
              size="small"
              variant="ghost"
              disabled={controller === null}
              onClick={clearTarget}
            >
              Clear
            </Button>
          ) : null}
        </div>
      </div>

      {controller?.setWorkingTargetStartHere !== undefined &&
      controller.setWorkingTargetEndHere !== undefined ? (
        <details className="wb-cowork-action-bar__range">
          <HelpTarget content={CURSOR_RANGE_HELP} placement="bottom end">
            <summary>
              Set by cursor
              {state.workingTargetStart === null ||
              state.workingTargetStart === undefined
                ? ""
                : ` · ${state.workingTargetStart.label}`}
            </summary>
          </HelpTarget>
          <div className="wb-cowork-action-bar__range-actions">
            <Button
              size="small"
              variant="ghost"
              disabled={state.phase !== "ready"}
              onClick={setRangeStart}
            >
              ↦ Set start
            </Button>
            <Button
              size="small"
              variant="ghost"
              disabled={
                state.phase !== "ready" ||
                state.workingTargetStart === null ||
                state.workingTargetStart === undefined
              }
              onClick={setRangeEnd}
            >
              ↤ Set end
            </Button>
            {state.workingTargetStart !== null &&
            state.workingTargetStart !== undefined &&
            controller.clearWorkingTargetDraft !== undefined ? (
              <Button size="small" variant="ghost" onClick={clearRange}>
                Cancel
              </Button>
            ) : null}
          </div>
        </details>
      ) : null}

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
