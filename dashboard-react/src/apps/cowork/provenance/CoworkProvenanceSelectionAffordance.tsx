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

export interface CoworkProvenanceSelectionAffordanceProps {
  readonly editor: Editor;
  /** Only the active Provenance lens may replace the general feedback action. */
  readonly active: boolean;
  readonly provider: ProvenanceProvider;
  readonly currentUserIdentity: CoworkProvenanceActorIdentity;
  readonly readOnly?: boolean;
  /** Authoritative editor/outbox state; survives mounting after the edit. */
  readonly inputProvenancePending?: boolean;
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
  if (intent === "review") return "Review provenance";
  if (intent === "view") return "View provenance";
  return "Inspect provenance";
};

const actionTitle = (intent: ProvenanceSelectionAction["intent"]): string => {
  if (intent === "record") {
    return "Record who wrote the selected text and whether it was reviewed; its earlier source remains untracked.";
  }
  if (intent === "review") {
    return "Open Provenance to confirm and record human review for this passage.";
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
  locallyDirty = false,
}: {
  readonly data: ProvenanceData;
  readonly doc: ProseMirrorNode;
  readonly from: number;
  readonly to: number;
  readonly readOnly: boolean;
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
        selectionExactlyMatchesTarget:
          resolution.from === from && resolution.to === to,
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
              selectionExactlyMatchesTarget: false,
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
  if (
    locallyDirty ||
    matches.length !== 1 ||
    !matches[0]!.completelyCoversSelection ||
    !targetIsHealthy(matches[0]!.target, matches[0]!.state)
  ) {
    return { intent: "inspect", targetIds };
  }

  const target = matches[0]!.target;
  const record = effectiveRecord(target)!;
  const canOfferReview =
    !readOnly &&
    target.reviewEligibility === "eligible" &&
    matches[0]!.selectionExactlyMatchesTarget &&
    (record.authorship.kind === "ai" || record.authorship.kind === "mixed") &&
    (record.humanReview.status === "not_reviewed" ||
      record.humanReview.status === "unknown");
  return {
    intent: canOfferReview ? "review" : "view",
    targetIds,
  };
};

export function CoworkProvenanceSelectionAffordance({
  editor,
  active,
  provider,
  currentUserIdentity,
  readOnly = false,
  inputProvenancePending = false,
  onRecord,
  onAction,
}: CoworkProvenanceSelectionAffordanceProps) {
  const [selection, setSelection] = useState<EditorSelection | null>(null);
  const [load, setLoad] = useState<ProvenanceLoad | null>(null);
  const requestSequence = useRef(0);
  const actionSequence = useRef(0);
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
      locallyDirty: locallyDirty || inputProvenancePending,
    });
  }, [
    anchor,
    editor,
    inputProvenancePending,
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
              inputProvenancePending
                ? "Co-work is recording provenance for recent typing."
                : actionTitle(classification.intent)
            }
            disabled={inputProvenancePending}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => {
              actionSequence.current += 1;
              const action: ProvenanceSelectionAction = {
                requestId: actionSequence.current,
                intent: classification.intent,
                anchor,
                from: selection.from,
                to: selection.to,
                targetIds: classification.targetIds,
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
            {inputProvenancePending ? "Recording recent typing…" : label}
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
