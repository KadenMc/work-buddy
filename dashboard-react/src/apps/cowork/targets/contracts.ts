/**
 * Consumer-neutral document targeting and exact action-capture contracts.
 *
 * A live editor selection, the reusable device-local document target, and the
 * immutable target captured for one action are intentionally different types.
 * None of these records grants readable context, change authority, or egress.
 */

import type { CoworkCompactionReceipt } from "../persistence/CoworkYdocPersistence";

export type CoworkActionTargetChoice =
  | "working_target"
  | "current_selection"
  | "current_section"
  | "custom_range"
  | "whole_document";

export type CoworkDocumentTargetResolution =
  | "relative"
  | "quote"
  | "unresolved";

export interface CoworkProseMirrorRange {
  /** Inclusive document boundary in the current ProseMirror document. */
  readonly from: number;
  /** Exclusive document boundary in the current ProseMirror document. */
  readonly to: number;
}

export interface CoworkRangeQuote {
  readonly exact: string;
  readonly prefix: string;
  readonly suffix: string;
}

/** Encoded Yjs RelativePositions for the range's first and last text positions. */
export interface CoworkRelativeRange {
  readonly startBase64: string;
  readonly endBase64: string;
}

export interface CoworkDocumentTargetReference {
  readonly schema: "wb.cowork.document-target/v1";
  readonly storeId: string;
  readonly documentId: string;
  readonly kind: "text_range";
  readonly relative: CoworkRelativeRange;
  readonly quote: CoworkRangeQuote;
  readonly label: string;
  readonly headingPath: readonly string[];
  readonly startBlockId?: string;
  readonly endBlockId?: string;
  readonly createdAt: string;
  readonly updatedAt: string;
}

export interface CoworkTargetSummary {
  readonly kind: "document" | "text_range" | "unresolved";
  readonly label: string;
  readonly wordCount: number;
  readonly range: CoworkProseMirrorRange | null;
  readonly resolution?: CoworkDocumentTargetResolution;
}

/**
 * Truth's canonical selector uses Unicode code-point offsets into the frozen
 * Markdown projection. It never uses ProseMirror positions.
 */
export type CoworkCanonicalTargetSelector =
  | { readonly kind: "document" }
  | {
      readonly kind: "text_quote";
      readonly exact: string;
      readonly prefix: string;
      readonly suffix: string;
      readonly start: number;
      readonly end: number;
    };

export interface CoworkResolvedActionTarget {
  readonly source: CoworkActionTargetChoice;
  readonly label: string;
  readonly wordCount: number;
  readonly proseMirrorRange: CoworkProseMirrorRange | null;
  readonly selector: CoworkCanonicalTargetSelector;
  readonly targetTextSha256: string;
  /**
   * Stable Yjs range identity for scoped actions. It is carried into the
   * immutable server action snapshot so a later recheck can resolve the same
   * logical passage after an accepted correction changes its text.
   */
  readonly targetReference?: CoworkDocumentTargetReference;
}

/**
 * Immutable browser capture handed to the injected Verify boundary. Binary Yjs
 * data is base64 so callers cannot mutate an otherwise-frozen Uint8Array.
 */
export interface CoworkCapturedActionSnapshot {
  readonly schema: "wb.cowork.action-snapshot/v1";
  readonly captureId: string;
  readonly storeId: string;
  readonly documentId: string;
  readonly capturedAt: string;
  readonly editGeneration: number;
  readonly ydocGenerationSha256: string;
  readonly snapshotBase64: string;
  readonly snapshotSha256: string;
  readonly stateVectorBase64: string;
  readonly stateVectorSha256: string;
  readonly structuredHeadSha256: string;
  readonly projectionMarkdown: string;
  readonly projectionSha256: string;
  readonly target: CoworkResolvedActionTarget;
}

export interface CoworkActionSnapshotControllerState {
  readonly phase: "loading" | "ready";
  readonly selection: CoworkTargetSummary | null;
  readonly currentSection: CoworkTargetSummary | null;
  readonly workingTarget: CoworkTargetSummary;
  /** Ephemeral accessible range-boundary workflow; it does not replace Working on. */
  readonly customRangeStart?: CoworkTargetSummary | null;
  readonly customRange?: CoworkTargetSummary | null;
}

export interface CoworkActionSnapshotController {
  readonly getSnapshot: () => CoworkActionSnapshotControllerState;
  readonly subscribe: (
    listener: () => void,
  ) => () => void;
  setWorkingTargetFromSelection(): void;
  clearWorkingTarget(): void;
  setCustomRangeStartHere?(): void;
  setCustomRangeEndHere?(): void;
  clearCustomRange?(): void;
  capture(
    choice: CoworkActionTargetChoice,
  ): Promise<CoworkCapturedActionSnapshot>;
  /**
   * Capture a server-projected durable target reference without changing the
   * user's current selection, custom range, or reusable Working on target.
   */
  captureReference?(
    choice: CoworkActionTargetChoice,
    reference: CoworkDocumentTargetReference | null,
  ): Promise<CoworkCapturedActionSnapshot>;
}

/** Narrow persistence surface used by capture; the full controller never leaves the editor. */
export interface CoworkActionCapturePersistence {
  readonly lastError: unknown;
  readonly pendingBatchCount: number;
  readonly docSha256: string;
  readonly ydocGeneration: string;
  retry(): Promise<void>;
  flush(): Promise<void>;
  compact(): Promise<CoworkCompactionReceipt>;
}

export interface CoworkVerifyRunIntent {
  readonly userGoal: string;
  readonly protectedIntent: string;
}

export type CoworkRunVerifyHandler = (
  capture: CoworkCapturedActionSnapshot,
  intent: CoworkVerifyRunIntent,
) => void | Promise<void>;

export type CoworkInvitePerspectiveHandler = (
  capture: CoworkCapturedActionSnapshot,
) => void | Promise<void>;
