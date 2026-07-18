/**
 * The Review rail provider seam. A live transport maps load onto R2 doc-get and
 * submitSitting onto R5 marks, and subscribe onto the SSE nudge (section 1.11).
 * The rail talks only to this seam for its review data plus the ledger sitting,
 * exactly as section 5.2 routes the Yjs binary and the sitting on the direct
 * route rather than through a ViewProvider snapshot. This module ships the
 * interface, an in-memory fixture supplies a deterministic implementation.
 */

import type {
  ReviewRailData,
  SittingResult,
  StagedClaimDecision,
  StagedDecision,
} from "./contracts";

/** Tear down a subscription registered through the provider. */
export type ReviewUnsubscribe = () => void;

/** Called by a provider when its view of the review layer may have changed. */
export type ReviewInvalidationListener = () => void;

/**
 * One sitting submission, the R5 request body in rail terms. proposalDecisions
 * are the per-item gestures, claimDecisions ride the same submit (a live
 * provider composes the claim path behind this seam).
 */
export interface SittingSubmission {
  readonly baseDocSha256: string;
  readonly proposalDecisions: readonly StagedDecision[];
  readonly claimDecisions: readonly StagedClaimDecision[];
}

/** The read and submit seam for one document's review layer. */
export interface ReviewRailProvider {
  /** Load the current review layer for the bound document (R2 doc-get). */
  load(): Promise<ReviewRailData>;
  /**
   * Register an invalidation listener, the SSE-nudge shape. The consumer
   * reloads on notify. The returned unsubscribe stops delivery.
   */
  subscribe(onInvalidate: ReviewInvalidationListener): ReviewUnsubscribe;
  /** Submit the staged sitting (R5 marks). The route mints the gestures. */
  submitSitting(submission: SittingSubmission): Promise<SittingResult>;
}

/**
 * The anchor-rect seam for the aligned-stream layout. The editor owns the live
 * ProseMirror decorations, so it is the only source that can report where a
 * proposal's anchor currently sits. The rail measures card heights itself and
 * asks this seam for anchor tops, then resolves overlaps outside the React
 * render cycle (audit A12, perf contract). When no source is wired the stream
 * degrades to a document-order list with scroll-to-and-highlight on select.
 */
export interface AnchorRectSource {
  /**
   * The top offset and height of a proposal's anchor, in the same coordinate
   * space as the rail scroll container, or null when the anchor is not
   * currently laid out (off-screen, lost, or the editor is not mounted).
   */
  anchorRect(proposalId: string): { readonly top: number; readonly height: number } | null;
  /** Bring a proposal's anchor into view and flash it (the degrade path). */
  scrollToAnchor(proposalId: string): void;
  /**
   * Register a listener fired whenever anchor geometry may have changed (editor
   * scroll, resize, or a decoration rebuild). Returns an unsubscribe.
   */
  subscribe(onGeometryChange: () => void): ReviewUnsubscribe;
}
