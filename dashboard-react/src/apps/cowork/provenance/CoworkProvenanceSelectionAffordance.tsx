import { useEffect, useMemo, useRef, useState } from "react";
import type { Editor } from "@tiptap/core";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";

import { quoteAnchorFromRange } from "../feedback/feedbackAnchor";
import { resolveProvenanceQuoteAnchorDetailed } from "../suggestions/anchor";
import { coworkPastePassageExcerpt } from "./pasteProvenance";
import { CoworkProvenanceDeterminationDialog } from "./CoworkProvenanceDeterminationDialog";
import {
  defaultCoworkProvenanceDetermination,
  type CoworkProvenanceActorIdentity,
  type CoworkProvenanceDetermination,
} from "./contracts";
import type {
  ProvenanceData,
  ProvenanceLoad,
  ProvenanceProvider,
  ProvenanceSelectionAction,
  ProvenanceTarget,
} from "./view/contracts";
import "./styles.css";

interface FloatPosition {
  readonly left: number;
  readonly top: number;
}

interface EditorSelection {
  readonly from: number;
  readonly to: number;
  readonly position: FloatPosition | null;
}

interface SelectionClassification {
  readonly intent: ProvenanceSelectionAction["intent"];
  readonly targetIds: readonly string[];
}

let provenanceSelectionRequestSequence = 0;

const nextProvenanceSelectionRequestId = (): number => {
  provenanceSelectionRequestSequence += 1;
  return provenanceSelectionRequestSequence;
};

export interface CoworkProvenanceSelectionAffordanceProps {
  readonly editor: Editor;
  /** Only the active Provenance lens may replace the general feedback action. */
  readonly active: boolean;
  readonly provider: ProvenanceProvider;
  readonly currentUserIdentity: CoworkProvenanceActorIdentity;
  readonly readOnly?: boolean;
  /**
   * Persists a user-confirmed attestation for a frozen uncovered selection.
   * The caller owns persistence settlement, target/head checks, and refresh.
   */
  readonly onRecord: (
    anchor: ProvenanceSelectionAction["anchor"],
    determination: CoworkProvenanceDetermination,
  ) => Promise<void>;
  /** Routes review/view/inspect into stable Provenance detail. */
  readonly onAction: (
    action: ProvenanceSelectionAction & {
      readonly intent: "review" | "view" | "inspect";
    },
  ) => void;
}

const actionLabel = (intent: ProvenanceSelectionAction["intent"]): string => {
  if (intent === "record") return "Record provenance";
  if (intent === "review") return "Mark as reviewed";
  if (intent === "view") return "View provenance";
  return "Inspect provenance";
};

const actionTitle = (intent: ProvenanceSelectionAction["intent"]): string => {
  if (intent === "record") {
    return "Record who wrote the selected text and whether it was reviewed; its earlier source remains untracked.";
  }
  if (intent === "review") {
    return "Open Provenance to record your review for the eligible selected passages.";
  }
  if (intent === "view") {
    return "Open the provenance details for this passage.";
  }
  return "Inspect the provenance records and targeting issues for this selection.";
};

const computeFloatPosition = (
  editor: Editor,
  to: number,
): FloatPosition | null => {
  try {
    const coords = editor.view.coordsAtPos(to);
    const host =
      (editor.view.dom.closest(".wb-cowork-editor") as HTMLElement | null) ??
      editor.view.dom;
    const rect = host.getBoundingClientRect();
    return {
      left: coords.left - rect.left,
      top: coords.bottom - rect.top,
    };
  } catch {
    return null;
  }
};

const effectiveRecord = (target: ProvenanceTarget) =>
  target.resolution === "resolved" ? target.effectiveAttestation : null;

const targetIsHealthy = (
  target: ProvenanceTarget,
  rangeState: "document" | "unique",
): boolean =>
  target.resolution === "resolved" &&
  target.target.currentness === "current" &&
  (target.target.kind === "document_version" || rangeState === "unique") &&
  effectiveRecord(target) !== null;

const selectionCoversDocumentText = (
  doc: ProseMirrorNode,
  from: number,
  to: number,
): boolean => {
  let firstTextPosition: number | null = null;
  let lastTextPosition: number | null = null;
  doc.descendants((node, position) => {
    if (!node.isText) return true;
    firstTextPosition ??= position;
    lastTextPosition = position + node.nodeSize;
    return false;
  });
  return (
    firstTextPosition !== null &&
    lastTextPosition !== null &&
    from <= firstTextPosition &&
    to >= lastTextPosition
  );
};

/**
 * Classify a selection against the same authoritative projection the panel
 * consumes. Explicit span targets take precedence over a document fallback.
 */
export const classifyCoworkProvenanceSelection = ({
  data,
  doc,
  from,
  to,
  readOnly,
  currentUserIdentity,
  locallyDirty = false,
}: {
  readonly data: ProvenanceData;
  readonly doc: ProseMirrorNode;
  readonly from: number;
  readonly to: number;
  readonly readOnly: boolean;
  readonly currentUserIdentity: CoworkProvenanceActorIdentity;
  readonly locallyDirty?: boolean;
}): SelectionClassification => {
  const explicit = data.spans.flatMap((target) => {
    if (target.span === null) return [];
    const resolution = resolveProvenanceQuoteAnchorDetailed(doc, target.span);
    if (
      resolution.state !== "unique" ||
      resolution.from >= to ||
      resolution.to <= from
    ) {
      return [];
    }
    return [
      {
        target,
        state: "unique" as const,
        completelyCoversSelection:
          resolution.from <= from && resolution.to >= to,
        selectionCompletelyCoversTarget:
          from <= resolution.from && to >= resolution.to,
        from: resolution.from,
        to: resolution.to,
      },
    ];
  });
  const matches =
    explicit.length > 0
      ? explicit
      : data.documentDefault === null ||
          !targetIsHealthy(data.documentDefault, "document")
        ? []
        : [
            {
              target: data.documentDefault,
              state: "document" as const,
              completelyCoversSelection: true,
              selectionCompletelyCoversTarget: selectionCoversDocumentText(
                doc,
                from,
                to,
              ),
              from: null,
              to: null,
            },
          ];
  const targetIds = [
    ...new Set(matches.map(({ target }) => target.projectionId)),
  ];

  if (matches.length === 0) {
    return {
      intent: readOnly || locallyDirty ? "inspect" : "record",
      targetIds: [],
    };
  }
  const overlappingTargets = matches.some(
    (candidate, index) =>
      candidate.from !== null &&
      candidate.to !== null &&
      matches.slice(index + 1).some(
        (peer) =>
          peer.from !== null &&
          peer.to !== null &&
          candidate.from! < peer.to &&
          candidate.to! > peer.from,
      ),
  );
  if (
    locallyDirty ||
    overlappingTargets ||
    matches.some(
      (match) => !targetIsHealthy(match.target, match.state),
    )
  ) {
    return { intent: "inspect", targetIds };
  }

  const needsCurrentUserReview = (target: ProvenanceTarget): boolean => {
    const record = effectiveRecord(target);
    if (
      record === null ||
      !["eligible", "already_reviewed"].includes(target.reviewEligibility) ||
      (record.authorship.kind !== "ai" && record.authorship.kind !== "mixed")
    ) {
      return false;
    }
    return !record.humanReview.reviewers.some(
      (reviewer) =>
        reviewer.ref === currentUserIdentity.ref &&
        reviewer.identityStatus === currentUserIdentity.identity_status,
    );
  };
  const reviewNeeded = matches.filter(({ target }) =>
    needsCurrentUserReview(target),
  );
  const reviewTargets = reviewNeeded.filter(
    ({ selectionCompletelyCoversTarget }) => selectionCompletelyCoversTarget,
  );
  if (
    !readOnly &&
    reviewTargets.length > 0
  ) {
    return {
      intent: "review",
      targetIds: reviewTargets.map(({ target }) => target.projectionId),
    };
  }
  if (
    matches.length !== 1 ||
    !matches[0]!.completelyCoversSelection
  ) {
    return { intent: "inspect", targetIds };
  }
  return {
    intent: "view",
    targetIds,
  };
};

export function CoworkProvenanceSelectionAffordance({
  editor,
  active,
  provider,
  currentUserIdentity,
  readOnly = false,
  onRecord,
  onAction,
}: CoworkProvenanceSelectionAffordanceProps) {
  const [selection, setSelection] = useState<EditorSelection | null>(null);
  const [load, setLoad] = useState<ProvenanceLoad | null>(null);
  const requestSequence = useRef(0);
  const [locallyDirty, setLocallyDirty] = useState(false);
  const [recording, setRecording] = useState<{
    readonly action: ProvenanceSelectionAction;
    readonly value: CoworkProvenanceDetermination;
  } | null>(null);
  const [recordBusy, setRecordBusy] = useState(false);
  const [recordError, setRecordError] = useState<string | null>(null);

  useEffect(() => {
    if (!active) {
      setSelection(null);
      return undefined;
    }
    const sync = (): void => {
      const { from, to, empty } = editor.state.selection;
      setSelection(
        empty || to <= from
          ? null
          : { from, to, position: computeFloatPosition(editor, to) },
      );
    };
    sync();
    editor.on("selectionUpdate", sync);
    return () => {
      editor.off("selectionUpdate", sync);
    };
  }, [active, editor]);

  useEffect(() => {
    const dirty = (): void => setLocallyDirty(true);
    editor.on("update", dirty);
    return () => {
      editor.off("update", dirty);
    };
  }, [editor]);

  useEffect(() => {
    if (!active) {
      setLoad(null);
      return undefined;
    }
    let mounted = true;
    const loadProjection = (): void => {
      const sequence = ++requestSequence.current;
      void provider.load().then(
        (next) => {
          if (mounted && sequence === requestSequence.current) {
            setLoad(next);
            setLocallyDirty(false);
          }
        },
        () => {
          if (mounted && sequence === requestSequence.current) setLoad(null);
        },
      );
    };
    loadProjection();
    const unsubscribe = provider.subscribe(loadProjection);
    return () => {
      mounted = false;
      requestSequence.current += 1;
      unsubscribe();
    };
  }, [active, provider]);

  const anchor = useMemo(
    () =>
      selection === null
        ? null
        : quoteAnchorFromRange(editor.state.doc, selection.from, selection.to),
    [editor, selection],
  );
  const classification = useMemo(() => {
    if (
      selection === null ||
      anchor === null ||
      load === null ||
      load.state !== "ready"
    ) {
      return null;
    }
    return classifyCoworkProvenanceSelection({
      data: load.data,
      doc: editor.state.doc,
      from: selection.from,
      to: selection.to,
      readOnly,
      currentUserIdentity,
      locallyDirty,
    });
  }, [
    anchor,
    currentUserIdentity,
    editor,
    load,
    locallyDirty,
    readOnly,
    selection,
  ]);

  if (
    recording === null &&
    (!active ||
      selection === null ||
      anchor === null ||
      classification === null)
  ) {
    return null;
  }
  const label =
    classification === null ? null : actionLabel(classification.intent);
  const style =
    selection?.position === null || selection?.position === undefined
      ? undefined
      : { left: selection.position.left, top: selection.position.top };
  return (
    <>
      {selection === null ||
      anchor === null ||
      classification === null ||
      label === null ? null : (
        <div className="wb-cowork-provenance-selection" style={style}>
          <button
            type="button"
            className="wb-cowork-provenance-selection__trigger"
            title={
              actionTitle(classification.intent)
            }
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => {
              const action: ProvenanceSelectionAction = {
                // The panel survives lens changes while this affordance does
                // not. A module-lifetime sequence prevents a remount from
                // reusing a completed action's identity.
                requestId: nextProvenanceSelectionRequestId(),
                intent: classification.intent,
                anchor,
                from: selection.from,
                to: selection.to,
                targetIds: classification.targetIds,
                coversWholeDocument: selectionCoversDocumentText(
                  editor.state.doc,
                  selection.from,
                  selection.to,
                ),
                ...(classification.intent === "review"
                  ? {
                      reviewer: {
                        ref: currentUserIdentity.ref,
                        identityStatus: currentUserIdentity.identity_status,
                      },
                    }
                  : {}),
              };
              if (action.intent === "record") {
                setRecordError(null);
                setRecording({
                  action,
                  value:
                    defaultCoworkProvenanceDetermination(currentUserIdentity),
                });
                return;
              }
              onAction({ ...action, intent: action.intent });
            }}
          >
            {label}
          </button>
        </div>
      )}
      {recording === null ? null : (
        <CoworkProvenanceDeterminationDialog
          value={recording.value}
          currentUserIdentity={currentUserIdentity}
          title="Record provenance"
          description="Record who wrote this selected text and whether it was reviewed. Its earlier source remains untracked."
          passageExcerpt={coworkPastePassageExcerpt(
            recording.action.anchor.exact,
          )}
          passageLabel="Selected passage"
          confirmLabel="Record provenance"
          cancelLabel="Cancel"
          busy={recordBusy}
          error={recordError}
          onChange={(value) =>
            setRecording((current) =>
              current === null ? null : { ...current, value },
            )
          }
          onClose={() => {
            if (!recordBusy) {
              setRecording(null);
              setRecordError(null);
            }
          }}
          onConfirm={async (value) => {
            if (recordBusy) return;
            setRecordBusy(true);
            setRecordError(null);
            try {
              await onRecord(recording.action.anchor, value);
              setRecording(null);
            } catch (cause) {
              setRecordError(
                cause instanceof Error
                  ? cause.message
                  : "Provenance could not be recorded.",
              );
            } finally {
              setRecordBusy(false);
            }
          }}
        />
      )}
    </>
  );
}

export default CoworkProvenanceSelectionAffordance;
