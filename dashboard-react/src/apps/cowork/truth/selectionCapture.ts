import type { CoworkCapturedActionSnapshot } from "../targets";

import type { TruthSelectionCapture } from "./contracts";

/**
 * Narrow the shared immutable editor capture to Truth's non-empty text range.
 * Keeping this adapter here prevents the panel from learning about Yjs bytes or
 * ProseMirror positions.
 */
export const truthSelectionCaptureFromActionSnapshot = (
  capture: CoworkCapturedActionSnapshot,
): TruthSelectionCapture => {
  if (capture.target.selector.kind !== "text_quote") {
    throw new Error("Select some text in the editor, then try again.");
  }
  return {
    schema: "wb.cowork.truth-selection/v1",
    captureId: capture.captureId,
    storeId: capture.storeId,
    documentId: capture.documentId,
    structuredHeadSha256: capture.structuredHeadSha256,
    ydocGenerationSha256: capture.ydocGenerationSha256,
    projectionSha256: capture.projectionSha256,
    label: capture.target.label,
    wordCount: capture.target.wordCount,
    selector: capture.target.selector,
  };
};
