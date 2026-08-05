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
  VerifyCheckInput,
  VerifyRunInspection,
  VerifyCriterionDraftInput,
} from "./contracts";
import type { RailSelectionKind } from "./store";

/**
 * Editor-anchor namespaces shared by Review and Truth. The historical name is
 * retained at the provider seam while the controller serves both rail views.
 */
export type ReviewAnchorKind =
  | RailSelectionKind
  | "expression"
  | "provenance"
  | "evaluation_result";

/** Tear down a subscription registered through the provider. */
export type ReviewUnsubscribe = () => void;

/** Called by a provider when its view of the review layer may have changed. */
export type ReviewInvalidationListener = () => void;

/**
 * One sitting submission in rail terms. proposalDecisions map to the live R5
 * request. claimDecisions remain part of the fixture-facing seam, but the live
 * provider fails closed until R2 supplies enough claim payload to implement
 * truthful claim semantics; it never silently drops or partially submits them.
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
  /** Immediate, exact-hash-bound action on a non-evidential Co-think item. */
  actOnCothink?(
    itemId: string,
    action: "park" | "dismiss",
    canonicalSha256: string,
  ): Promise<void>;
  /** Save an exact item/action-bound Co-think turn into document Chat. */
  discussCothink?(
    itemId: string,
    canonicalSha256: string,
  ): Promise<{
    readonly conversationId: string;
    readonly messageId: string;
  }>;
  /** Append an exact-document human activation and wait for the fresh projection. */
  setVerifyCriterionEnabled?(
    criterionKey: string,
    enabled: boolean,
    expectedActivationId: string | null,
  ): Promise<void>;
  /** Inspect one run's immutable typed records without raw worker prose. */
  inspectVerifyRun?(runId: string): Promise<VerifyRunInspection>;
  /** Save an unavailable user-authored criterion/checker draft for later admission. */
  createVerifyCriterionDraft?(
    draft: VerifyCriterionDraftInput,
  ): Promise<void>;
  /** Create, enable, and reload a declarative check backed by an admitted mechanism. */
  createVerifyCheck?(check: VerifyCheckInput): Promise<void>;
}

/** How a one-shot Review activation should reveal its target in the editor. */
export interface AnchorRevealOptions {
  /** Briefly flash the anchor in addition to its persistent focused treatment. */
  readonly flash?: boolean;
}

/**
 * The editor-owned Review-anchor seam. Review cards remain in normal document
 * order; this controller owns focused passage treatment and explicit passage
 * navigation. Those are deliberately separate operations: persistent focus is
 * safe to replay after projection refreshes, while a reveal is a one-shot user
 * command and must never become refreshable state.
 */
export interface ReviewAnchorController {
  /**
   * Persistently emphasize the selected Review target. The kind is part of the
   * identity: claim ids and proposal ids occupy separate namespaces. Filtering
   * the rail never removes editor annotations; it can only move or clear this
   * focused treatment.
   */
  focusAnchor(
    id: string,
    kind: ReviewAnchorKind,
  ): void;
  /**
   * Focus and bring one target into view because the user activated a Review
   * card, moved through Queue, or used an explicit passage affordance. A
   * projection refresh may restore the focus treatment but must not replay this
   * navigation.
   */
  revealAnchor(
    id: string,
    kind: ReviewAnchorKind,
    options?: AnchorRevealOptions,
  ): void;
  /** Clear only the focused treatment, never the underlying annotations. */
  clearFocusedAnchor(): void;
}
