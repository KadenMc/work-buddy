/**
 * Data contracts for the Co-work Review rail (C1 surface contract sections 1.3,
 * 1.5, and 5). Everything the rail renders arrives through the read-only
 * ReviewRailProvider seam, and everything the human stages leaves through the
 * sitting submission seam. These types mirror the R2 doc-get payload and the R5
 * marks vocabulary in a JSON-compatible shape, so a live transport can populate
 * them without any shape change. No HTTP wiring lives here.
 */

import type {
  CoworkActionTargetChoice,
  CoworkCanonicalTargetSelector,
  CoworkDocumentTargetReference,
} from "../targets";

/** Ledger-canonical trust state for a rendered proposal (PRD section 7 tri-state). */
export type CoworkEpistemicState = "ai_proposed" | "ai_confirmed" | "human";

/** A proposal is either a tracked edit or a flag with no replacement (PRD section 6). */
export type ProposalKind = "edit" | "flag";

/**
 * Display classification for an edit proposal. Derived from the suggestion
 * transaction at ingestion, carried here so the stream and queue can label and
 * colour a card without re-deriving. Flags carry no change type.
 */
export type ProposalChangeType = "insertion" | "deletion" | "modification";

/** Open lifecycle status a proposal can hold while it still awaits a gesture. */
export type ProposalStatus = "open" | "redraft_pending";

/** How a passage expresses a claim (S2 or S7, the one claim_refs role vocabulary). */
export type ClaimRefRole = "quote" | "paraphrase" | "summary" | "instantiation";

/** Web-Annotation style quote anchor resolved by the kernel, never by node id. */
export interface QuoteAnchor {
  readonly exact: string;
  readonly prefix: string;
  readonly suffix: string;
}

/** Producing run identity that survives acceptance (I11 provenance). */
export interface ProposalProducer {
  readonly model: string;
  readonly modelSource: string;
  readonly sessionId: string;
  readonly surface: string;
}

/** One claim reference on a proposal (S7 one shape everywhere). */
export interface ProposalClaimRef {
  readonly claim: string;
  readonly role: ClaimRefRole;
}

/**
 * One open proposal as delivered by R2 doc-get, plus two rail-display fields
 * (anchorLabel, documentOrder) the stream and queue use for ordering and the
 * scroll-to-anchor affordance. base_ok is the S6 stale-base signal.
 */
export interface ReviewProposal {
  readonly proposalId: string;
  readonly kind: ProposalKind;
  /** Present for edit proposals, absent for flags. */
  readonly changeType?: ProposalChangeType;
  readonly quoteAnchor: QuoteAnchor;
  /** The replacement text for an edit, null for a flag. */
  readonly replacement: string | null;
  readonly rationale: string;
  readonly tldr: string;
  readonly producer: ProposalProducer;
  readonly epistemicState: CoworkEpistemicState;
  readonly baseDocSha256: string;
  /** The per-item hash the human is shown, the single-use gesture binding (I6). */
  readonly canonicalSha256: string;
  /** S6: false marks the proposal stale-base, decidable only via reject or defer. */
  readonly baseOk: boolean;
  readonly status: ProposalStatus;
  /** Set when a flag was endorsed and the drafted fix returned as a linked proposal. */
  readonly fixesRef: string | null;
  readonly claimRefs: readonly ProposalClaimRef[];
  readonly createdAt: string;
  /** Document-order label for the anchor, e.g. "paragraph 2". */
  readonly anchorLabel: string;
  /** Monotonic document position used to order the stream and the queue. */
  readonly documentOrder: number;
}

/** Status of the claim a passage expresses, for the read-path chip. */
export type ExpressionClaimStatus =
  | "confirmed"
  | "needs_review"
  | "proposed"
  | "rejected";

/** One expression row, the claim underneath a passage (PRD section 5 read path). */
export interface ReviewExpression {
  readonly expressionId: string;
  readonly spanId: string;
  readonly nodeIdHint: string | null;
  readonly quote: string;
  readonly quoteAnchor?: QuoteAnchor;
  readonly claimRef: string;
  readonly claimStatus: ExpressionClaimStatus | null;
  readonly claimKind: string | null;
}

/** The three v1 provenance states enumerable from the ledger (PRD section 7). */
export type TrustState = "human" | "ai_confirmed" | "ai_proposed";

/** One provenance span for the inspector, re-anchored by quote (I12). */
export interface ProvenanceSpan {
  readonly spanId: string;
  readonly quote: string;
  readonly quoteAnchor?: QuoteAnchor;
  readonly trustState: TrustState;
  readonly producer: ProposalProducer | null;
  readonly approvalGestureId: string | null;
}

/** Claim lifecycle status shown on a claim-review card (kernel claim states). */
export type ClaimStatus =
  | "proposed"
  | "confirmed"
  | "challenged"
  | "rejected"
  | "superseded"
  | "retracted"
  | "expired";

/** One active evidence receipt shown with a claim (kernel review receipt). */
export interface ClaimReceipt {
  readonly evidenceId: string;
  readonly quote: string;
  readonly sourceLocator: string;
  readonly trustClass: string;
}

/**
 * One claim for the claims tab. Delivered through the review provider seam (a
 * live provider maps the kernel review payloads onto it), so the shape carries
 * just what the card and the six claim verbs need.
 */
export interface ReviewClaim {
  readonly claimId: string;
  readonly proposition: string;
  readonly status: ClaimStatus;
  readonly claimKind: string;
  readonly canonicalSha256: string;
  readonly rationale: string;
  readonly receipts: readonly ClaimReceipt[];
  /** Document-order anchor label so a claim card can point back into the prose. */
  readonly anchorLabel: string;
  readonly documentOrder: number;
}

/** Capability negotiation for the additive Co-work Verify + Co-think contract. */
export interface CoworkVerifyCapability {
  readonly enabled: boolean;
  readonly contractVersion: number;
  readonly canRun: boolean;
  readonly canConfigure: boolean;
  readonly canCothink: boolean;
  readonly disabledReason: string | null;
}

export interface VerifyActor {
  readonly kind: string;
  readonly ref: string | null;
  readonly meta: Readonly<Record<string, unknown>> | null;
}

export interface VerificationCheck {
  readonly id: string;
  readonly stableKey: string;
  readonly version: number;
  readonly title: string;
  readonly mechanism: string;
  readonly executorRef: string;
  readonly limitations: readonly string[];
  readonly definitionOrigin: string;
  readonly author: VerifyActor;
  readonly dataSharingClass: string;
  readonly externalEgress: boolean | null;
  readonly dataSharingBasis: string;
  readonly availability: "available" | "unavailable";
  readonly unavailableReason: string | null;
  readonly executionLocation: string | null;
  readonly bindingId: string;
  readonly selected: boolean;
  readonly configuration: Readonly<Record<string, unknown>>;
}

export interface VerificationCriterion {
  readonly id: string;
  readonly stableKey: string;
  readonly version: number;
  readonly title: string;
  readonly description: string;
  readonly kind: string;
  readonly definitionOrigin: string;
  readonly author: VerifyActor;
  readonly activationId: string | null;
  readonly enabled: boolean;
  readonly required: boolean;
  readonly locked: boolean;
  readonly activationOrigin: string | null;
  readonly authorizedBy: VerifyActor | null;
  readonly operationalState:
    | "active"
    | "inactive"
    | "unavailable"
    | "blocked_required_check";
  readonly availableCheckCount: number;
  readonly totalCheckCount: number;
  readonly checks: readonly VerificationCheck[];
  readonly issues: readonly Readonly<Record<string, unknown>>[];
}

export type VerifyCostEnforcementClass =
  | "hard_ceiling"
  | "estimate"
  | "unavailable";

export interface VerifyProviderCostControl {
  readonly providerId: string;
  readonly enforcementClass: VerifyCostEnforcementClass;
  readonly ceilingUsdPerWorkerSession: number | null;
  readonly basis: string;
}

export interface VerifyExecutionPlan {
  readonly schema: string;
  readonly authoritative: true;
  readonly checker: {
    readonly executionClass: "in_process";
    readonly mechanism: "deterministic_exact_match";
    readonly modelCall: false;
    readonly externalEgress: false;
    readonly contentBoundary: "captured_target";
  };
  readonly coordination: {
    readonly executionClass: "account_backed_agent";
    readonly selection: {
      readonly mode: "explicit_at_run_start";
      readonly providerId: string | null;
      readonly modelId: string | null;
      readonly providerLabel: string | null;
      readonly modelLabel: string | null;
    };
    readonly contentBoundary: "entire_frozen_document";
    readonly externalEgress: true;
    readonly fallback: {
      readonly providerModelFallback: false;
      readonly failureMode: "fail_closed";
    };
    readonly workerSessions: {
      readonly initial: number;
      readonly maximum: number;
      readonly conditionalRoles: readonly string[];
    };
    readonly costControl: VerifyProviderCostControl | null;
    readonly providerCostControls: readonly VerifyProviderCostControl[];
  };
}

/** The effective future-run setup for one document. */
export interface VerificationConfiguration {
  readonly schema: string;
  readonly documentId: string;
  readonly executionPlan: VerifyExecutionPlan | null;
  readonly coordination: {
    readonly deprecated?: boolean;
    readonly authoritativeProjection?: string;
    readonly required: boolean;
    readonly selection: string;
    readonly contentBoundary: string;
    readonly egressClass: string;
    readonly externalEgress: boolean;
    readonly costCeilingUsdPerWorker: number;
    readonly costCeilingSemantics?: string;
    readonly separateReviserForFindings: boolean;
    readonly pattern: string;
    readonly baseWorkerCalls: number;
    readonly maximumWorkerCalls: number;
  } | null;
  readonly criteria: readonly VerificationCriterion[];
}

export interface VerifyCriterionDraftInput {
  readonly title: string;
  readonly description: string;
  readonly evaluationInstructions: string;
  readonly limitations: readonly string[];
}

/** Declarative semantics for one runnable, user-authored Verify check. */
export interface VerifyCheckInput {
  readonly title: string;
  readonly description: string;
  readonly evaluationInstructions: string;
  readonly limitations: readonly string[];
}

export type EvaluationRunStatus =
  | "prepared"
  | "queued"
  | "running"
  | "completed"
  | "completed_with_failures"
  | "failed"
  | "cancelled";

export type EvaluationResultKind =
  | "conforming"
  | "nonconforming"
  | "inconclusive"
  | "review_comment";

export type EvaluationDisposition =
  | "surface_result"
  | "retain_without_interrupting"
  | "surface_proposal"
  | "suggest_cothink"
  | "defer_until_boundary"
  | "escalate";

/** One durable run summary. Quiet runs remain inspectable here. */
export interface EvaluationRunSummary {
  readonly runId: string;
  readonly status: EvaluationRunStatus;
  readonly purpose: string;
  readonly targetLabel: string;
  readonly coverageLabel: string;
  readonly currentVersion: boolean;
  readonly resultCount: number;
  readonly surfacedResultCount: number;
  readonly coordinationStatus: "pending" | "completed" | "unavailable";
  readonly providerLabel: string | null;
  readonly providerId: string | null;
  readonly modelLabel: string | null;
  readonly modelId: string | null;
  readonly createdAt: string;
  readonly finishedAt: string | null;
}

/** Safe run detail: immutable typed records only, never raw worker prose. */
export interface VerifyRunInspection {
  readonly schema: string;
  readonly runId: string;
  readonly action: {
    readonly actionSnapshotId: string;
    readonly structuredHeadSha256: string;
    readonly targetKind: string;
    readonly contextBoundary: Readonly<Record<string, unknown>>;
    readonly egressBoundary: Readonly<Record<string, unknown>>;
  };
  readonly plan: {
    readonly planSnapshotId: string;
    readonly canonicalSha256: string;
    readonly definition: Readonly<Record<string, unknown>>;
  };
  readonly checks: readonly {
    readonly checkExecutionId: string;
    readonly status: string;
    readonly mechanism: string;
    readonly definition: {
      readonly stableKey: string;
      readonly version: number;
      readonly title: string;
      readonly limitations: readonly string[];
    };
  }[];
  readonly results: readonly {
    readonly evaluationResultId: string;
    readonly kind: string;
    readonly message: string;
    readonly dispositions: readonly {
      readonly decision: string;
      readonly rationale: string;
      readonly policySnapshotSha256: string | null;
    }[];
    readonly lineage: readonly {
      readonly relation: string;
      readonly targetKind: string;
      readonly targetRef: string;
    }[];
  }[];
  readonly coordination: readonly {
    readonly jobId: string;
    readonly role: string;
    readonly status: string;
    readonly provider: string;
    readonly model: string;
    readonly egressClass: string;
    readonly costCeilingUsd: number;
    readonly executionPlan: VerifyExecutionPlan | null;
    readonly error: string | null;
  }[];
}

/** An immutable normalized observation that a coordinator chose to surface. */
export interface EvaluationResult {
  readonly resultId: string;
  readonly runId: string;
  readonly kind: EvaluationResultKind;
  readonly criterionLabel: string;
  readonly criterionStatement: string;
  readonly checkLabel: string;
  readonly methodLabel: string;
  readonly explanation: string;
  readonly quoteAnchor: QuoteAnchor | null;
  readonly coverageLabel: string;
  readonly limitations: readonly string[];
  readonly currentVersion: boolean;
  readonly disposition: EvaluationDisposition;
  readonly canonicalSha256: string;
  readonly proposalIds: readonly string[];
  readonly createdAt: string;
}

export type CothinkItemStatus = "open" | "parked" | "dismissed";

/**
 * A Co-think contribution is deliberately non-evidential. It therefore has no
 * pass/fail, severity, confidence, or proposal decision fields.
 */
export interface CothinkItem {
  readonly itemId: string;
  readonly subtype: "alternative_perspective";
  readonly content: string;
  readonly rationale: string;
  readonly targetLabel: string;
  readonly quoteAnchor: QuoteAnchor | null;
  readonly status: CothinkItemStatus;
  readonly currentVersion: boolean;
  readonly canonicalSha256: string;
  readonly createdAt: string;
}

export interface CothinkOutcome {
  readonly outcomeId: string;
  readonly status:
    | "running"
    | "completed_with_item"
    | "completed_no_useful_item"
    | "unavailable";
  readonly rationale: string;
  readonly targetLabel: string;
  readonly currentVersion: boolean;
  readonly providerId: string;
  readonly modelId: string;
  readonly createdAt: string;
  readonly finishedAt: string | null;
}

export type VerificationRecheckStatus =
  | "pending_capture"
  | "user_action_required"
  | "fulfilled";

/**
 * Restart-surviving intent derived from a committed sitting. A scoped intent
 * is runnable only with its exact original Yjs target reference; the client
 * must never substitute the current selection or the whole document.
 */
export interface VerificationRecheckIntent {
  readonly intentId: string;
  readonly sittingId: string;
  readonly sourceRunId: string;
  readonly proposalIds: readonly string[];
  readonly pendingProposalIds: readonly string[];
  readonly fulfilledByRunIds: readonly string[];
  readonly committedAt: string;
  readonly status: VerificationRecheckStatus;
  readonly userGoal: string;
  readonly protectedIntent: string;
  readonly originalActionTarget: {
    readonly actionSnapshotId: string;
    readonly source: CoworkActionTargetChoice | null;
    readonly label: string | null;
    readonly kind: "document" | "text_quote";
    readonly selector: CoworkCanonicalTargetSelector;
    readonly targetTextSha256: string;
    readonly targetReference: CoworkDocumentTargetReference | null;
    readonly targetReferenceSha256: string | null;
  };
  readonly execution: {
    readonly providerId: string;
    readonly modelId: string;
    readonly providerLabel: string;
    readonly modelLabel: string;
  };
  readonly requires: {
    readonly freshActionSnapshot: boolean;
    readonly freshModelCallAuthorization: boolean;
    readonly sameTargetSource: boolean;
    readonly sameTargetReference: boolean;
    readonly exactTargetResolution: boolean;
    readonly userAffirmedExactTargetRequired: boolean;
    readonly allowWidenToWholeDocument: false;
  };
}

/** Drift state plus open counts for the rail drift-health strip (R1 or R7). */
export interface RailDriftHealth {
  readonly state: "clean" | "drifted" | "missing";
  readonly openProposalCount: number;
  readonly openFlagCount: number;
  readonly lastMaterializedSha256: string | null;
  readonly currentFileSha256: string | null;
}

/** The full read-only review layer for one document (R2 doc-get, rail shape). */
export interface ReviewRailData {
  readonly documentId: string;
  readonly title: string;
  readonly drift: RailDriftHealth;
  readonly verifyCapability: CoworkVerifyCapability;
  readonly verificationConfiguration: VerificationConfiguration;
  readonly evaluationRuns: readonly EvaluationRunSummary[];
  readonly evaluationResults: readonly EvaluationResult[];
  readonly verificationRecheckIntents: readonly VerificationRecheckIntent[];
  readonly cothinkItems: readonly CothinkItem[];
  readonly cothinkOutcomes: readonly CothinkOutcome[];
  readonly proposals: readonly ReviewProposal[];
  readonly expressions: readonly ReviewExpression[];
  readonly provenanceSpans: readonly ProvenanceSpan[];
  readonly claims: readonly ReviewClaim[];
}

/** A shipped proposal or flag gesture-kind name (S1, the R5 wire verb). */
export type ProposalVerbKind =
  | "confirm"
  | "edit_confirm"
  | "reject_plain"
  | "reject_as_false"
  | "reject_as_preference"
  | "redirect"
  | "defer"
  | "endorse"
  | "dismiss";

/** The six committed claim verbs (kernel truth_claim_* capabilities). */
export type ClaimVerbKind =
  | "propose"
  | "confirm"
  | "reject"
  | "challenge"
  | "supersede"
  | "redact";

/**
 * One staged proposal or flag decision, the R5 item shape (section 1.5). It is
 * held locally until the human submits the sitting, and the route mints the
 * gesture, never the client.
 */
export interface StagedDecision {
  readonly proposalId: string;
  readonly verb: ProposalVerbKind;
  /** Echoes the shown hash, the I6 single-use binding. */
  readonly canonicalSha256: string;
  /** Required when verb is edit_confirm, the human replacement. */
  readonly amendContent?: string;
  /** Required when verb is redirect, the typed note to the agent. */
  readonly redirectNote?: string;
  /** reject_as_false only, verbatim negation when the proposal carries no claim_refs. */
  readonly negationText?: string;
  /** reject_as_preference only, the human's verbatim preferred phrasing (FA-1). */
  readonly preferenceText?: string;
}

/** One staged claim decision, carried on the same sitting submission. */
export interface StagedClaimDecision {
  readonly claimId: string;
  readonly verb: ClaimVerbKind;
  readonly canonicalSha256: string;
}

/** Per-item R5 result vocabulary (S4). Each verb maps to exactly one result. */
export type SittingResultKind =
  | "applied"
  | "closed"
  | "kept_open_redirected"
  | "kept_open_deferred"
  | "kept_open_endorsed"
  | "rejected_stale_view"
  | "error";

/** One per-item sitting result (R5 response, section 1.5). */
export interface SittingItemResult {
  readonly proposalId: string;
  readonly verb: ProposalVerbKind;
  readonly result: SittingResultKind;
  readonly baseOk: boolean;
  readonly gestureId: string | null;
  readonly error: string | null;
}

/** The R5 sitting response, per-item and never all-or-nothing (S4). */
export interface SittingResult {
  readonly ok: boolean;
  readonly partial: boolean;
  readonly results: readonly SittingItemResult[];
}
