import type { RefCallback } from "react";

import type { CoworkCapturedActionSnapshot } from "../targets";

/** The two truthful read scopes exposed by Co-work's Truth surface. */
export type TruthViewScope = "document" | "folder";

/**
 * Filters are semantic views over claim state. "facts" is intentionally not a
 * synonym for every claim: only current authoritative claims qualify. The
 * server owns that classification; the client never reconstructs it.
 */
export type TruthClaimFilter =
  | "all"
  | "facts"
  | "proposed"
  | "needs_review"
  | "challenged"
  | "unconnected";

export type TruthClaimBaseStatus =
  | "proposed"
  | "confirmed"
  | "rejected"
  | "expired"
  | "challenged"
  | "superseded"
  | "retracted"
  | "unknown";

export type TruthClaimHealth =
  | "clean"
  | "needs_review"
  | "conflict"
  | "failed"
  | "redacted"
  | "voided"
  | "unknown";

export type TruthExpressionRole =
  | "quote"
  | "paraphrase"
  | "summary"
  | "instantiation";

export type TruthClaimAction =
  | "confirm"
  | "reaffirm"
  | "reject"
  | "challenge"
  | "supersede"
  | "redact";

export interface TruthCapabilities {
  readonly canObserve: boolean;
  readonly canModify: boolean;
  readonly canDecide: boolean;
  readonly allowedClaimKinds: readonly string[];
  readonly mutationUnavailableReason: string | null;
}

export interface TruthQuoteSelector {
  readonly kind: "text_quote";
  readonly exact: string;
  readonly prefix: string;
  readonly suffix: string;
  readonly start?: number;
  readonly end?: number;
}

/** One exact document passage to which a claim is connected. */
export interface TruthPassageConnection {
  readonly expressionId: string;
  readonly spanId: string;
  readonly documentId: string;
  readonly documentTitle: string | null;
  readonly documentPath: string | null;
  readonly role: TruthExpressionRole;
  readonly quote: string;
  readonly selector: TruthQuoteSelector;
  readonly currentDocument: boolean;
  readonly claimCanonicalSha256: string;
  readonly createdAt: string;
  readonly createdBy: {
    readonly kind: string;
    readonly ref: string | null;
  } | null;
  readonly provenance?: TruthRecordProvenance;
}

export interface TruthAnalysisPreparationProvenance {
  readonly kind: "agent_run";
  readonly surface: "cowork_truth_analysis";
  readonly analysisRunId: string;
  readonly candidateId: string;
  readonly providerId: string;
  readonly modelId: string;
}

export interface TruthRecordProvenance {
  readonly preparedBy: TruthAnalysisPreparationProvenance | null;
  readonly addedBy: {
    readonly kind: string;
    readonly ref: string | null;
    readonly at: string;
  };
}

/**
 * Ephemeral handoff for one explicit cross-document passage navigation.
 * The widget owns this only while the registered document session changes;
 * it must never be persisted or replayed by a later projection refresh.
 */
export interface TruthPassageNavigationTarget {
  readonly requestId: string;
  readonly storeId: string;
  readonly documentId: string;
  readonly expressionId: string;
  readonly spanId: string;
  readonly selector: TruthQuoteSelector;
}

export interface TruthClaimSummary {
  readonly claimId: string;
  readonly proposition: string;
  readonly claimKind: string;
  readonly canonicalSha256: string;
  readonly scope: string;
  readonly baseStatus: TruthClaimBaseStatus;
  /** A review overlay is independent of the append-only base lifecycle. */
  readonly needsReview: boolean;
  readonly health: TruthClaimHealth;
  readonly healthReason: string | null;
  readonly voided: boolean;
  readonly redacted: boolean;
  readonly validFrom: string | null;
  readonly validTo: string | null;
  readonly effectiveValidFrom: string | null;
  readonly effectiveValidTo: string | null;
  readonly evidenceCount: number;
  readonly connectionCount: number;
  readonly connections: readonly TruthPassageConnection[];
  readonly createdAt: string;
  readonly createdBy?: {
    readonly kind: string;
    readonly ref: string | null;
  } | null;
  readonly provenance?: TruthRecordProvenance;
  /** Server-derived fact classification; clients use a conservative fallback. */
  readonly isFact: boolean;
  /** Authoritative actions for this claim in the current policy context. */
  readonly availableActions: readonly TruthClaimAction[];
}

export interface TruthEvidenceReceipt {
  readonly linkId: string;
  readonly spanId: string;
  readonly evidenceId: string;
  readonly evidenceKind: string;
  readonly quote: string | null;
  readonly sourceLocator: string;
  readonly trustClass: string;
  readonly authorKind: string | null;
  readonly authorRef: string | null;
  readonly active: boolean;
  readonly spanSha256: string;
  readonly contentSha256: string;
  readonly mediaType: string | null;
  readonly derivedFromStore: string | null;
  readonly acquiredAt: string | null;
  readonly acquisitionMethod: string | null;
  readonly spanRedactedAt: string | null;
  readonly evidenceRedactedAt: string | null;
  readonly integrity: {
    readonly state: string;
    readonly detail: string | null;
    readonly locatorScheme: string | null;
    readonly verifiabilityClass: string | null;
    readonly snapshotPresent: boolean;
  } | null;
}

export interface TruthLifecycleEvent {
  readonly eventId: string;
  readonly status: string;
  readonly at: string;
  readonly actorKind: string;
  readonly actorRef: string | null;
  readonly note: string | null;
}

export interface TruthConflict {
  readonly relationId: string;
  readonly claimId: string;
  readonly proposition: string | null;
  readonly status: string | null;
  readonly conflictType: string | null;
  readonly conflictClass: string | null;
  readonly direction: "challenges" | "challenged_by" | "unknown";
  readonly createdAt: string | null;
}

export interface TruthSupportAssessment {
  readonly supportSpanIds: readonly string[];
  readonly usableSpanIds: readonly string[];
  readonly quarantinedOnly: boolean;
  readonly agentAuthoredOnly: boolean;
  readonly storeDerivedOnly: boolean;
}

export interface TruthPremiseAssessment {
  readonly localUnconfirmed: readonly string[];
  readonly unresolvedUris: readonly string[];
  readonly confirmed: boolean;
}

export interface TruthDerivationPremise {
  readonly kind: string;
  readonly ref: string;
  readonly proposition: string | null;
  readonly status: string | null;
}

export interface TruthDerivation {
  readonly method: string;
  readonly rationale: string | null;
  readonly confidence: number | null;
  readonly premises: readonly TruthDerivationPremise[];
}

export interface TruthClaimDetail extends TruthClaimSummary {
  readonly structured: Readonly<Record<string, unknown>>;
  readonly receipts: readonly TruthEvidenceReceipt[];
  readonly lifecycle: readonly TruthLifecycleEvent[];
  readonly conflicts: readonly TruthConflict[];
  readonly derivations: readonly TruthDerivation[];
  readonly support: TruthSupportAssessment;
  readonly premises: TruthPremiseAssessment;
  /** Server-composed binding for an exact, guarded human decision. */
  readonly decisionBinding: {
    readonly payloadSha256: string;
    readonly contextSha256: string;
    readonly agentAuthoredOnly: boolean;
  } | null;
}

export interface TruthFilterCounts {
  readonly all: number;
  readonly facts: number;
  readonly proposed: number;
  readonly needsReview: number;
  readonly challenged: number;
  readonly unconnected: number;
}

export interface TruthClaimsSnapshot {
  readonly schema: string;
  readonly storeId: string;
  readonly documentId: string;
  readonly scope: TruthViewScope;
  readonly filter: TruthClaimFilter;
  readonly claims: readonly TruthClaimSummary[];
  readonly counts: TruthFilterCounts;
  readonly capabilities: TruthCapabilities;
  readonly readOnly: boolean;
  readonly nextOffset: number | null;
}

export interface TruthQuery {
  readonly scope: TruthViewScope;
  readonly filter: TruthClaimFilter;
}

/**
 * Exact, frozen selection captured by the editor integration before a Truth
 * mutation. The opaque target reference may be persisted by the server but is
 * never interpreted by the Truth UI.
 */
export interface TruthSelectionCapture {
  readonly schema: "wb.cowork.truth-selection/v1";
  readonly captureId: string;
  readonly storeId: string;
  readonly documentId: string;
  readonly structuredHeadSha256: string;
  readonly ydocGenerationSha256: string;
  readonly projectionSha256: string;
  readonly label: string;
  readonly wordCount: number;
  readonly selector: TruthQuoteSelector;
  readonly targetReference?: Readonly<Record<string, unknown>>;
}

export interface TruthProposeClaimRequest {
  readonly capture: TruthSelectionCapture;
  readonly proposition: string;
  readonly claimKind: string;
  readonly role: TruthExpressionRole;
}

export interface TruthConnectClaimRequest {
  readonly capture: TruthSelectionCapture;
  readonly claimId: string;
  readonly role: TruthExpressionRole;
}

export type TruthClaimDecision = "confirm" | "reaffirm" | "reject" | "redact";
export type TruthRedactionReason =
  | "privacy"
  | "source_takedown"
  | "rejected_content"
  | "expired_content";

export interface TruthClaimDecisionRequest {
  readonly claimId: string;
  readonly action: TruthClaimDecision;
  readonly expectedCanonicalSha256: string;
  readonly expectedContextSha256: string;
  readonly gestureKind?: "confirm" | "reaffirm";
  readonly reason?: TruthRedactionReason;
}

export interface TruthMutationReceipt {
  readonly ok: boolean;
  readonly claimId: string | null;
  /** Whether this request created the canonical claim instead of reusing it. */
  readonly claimCreated: boolean;
  readonly expressionId: string | null;
  /** Whether this request created the passage connection instead of reusing it. */
  readonly expressionCreated: boolean;
  readonly status: string | null;
}

/** Exact editor target accepted by the first AI-assisted Truth slice. */
export type TruthAnalysisTargetChoice =
  | "current_selection"
  | "working_target";

export type TruthAnalysisRunStatus =
  | "queued"
  | "running"
  | "completed"
  | "completed_with_failures"
  | "failed"
  | "cancelled";

export type TruthAnalysisCandidateStatus =
  | "pending"
  | "saved"
  | "dismissed";

export type TruthEvidenceRelationship =
  | "supports"
  | "partially_supports"
  | "contradicts"
  | "mentions"
  | "does_not_address"
  | "inconclusive";

/** Explicit account-backed execution selection frozen when a run starts. */
export interface TruthAnalysisExecutionSelection {
  readonly providerId: string;
  readonly modelId: string;
  readonly providerLabel: string;
  readonly modelLabel: string;
}

export interface TruthAnalysisExistingClaimMatch {
  readonly claimId: string;
  readonly proposition: string;
  readonly relationship:
    | "exact"
    | "equivalent"
    | "overlaps"
    | "conflicts"
    | "unknown";
  readonly confidence: number | null;
  readonly rationale: string | null;
}

export interface TruthAnalysisEvidenceCandidate {
  readonly evidenceCandidateId: string;
  readonly sourceKind: "truth_span" | "web_fetch" | "passage_citation";
  /** Server-authoritative admission flag; absent/false is never selectable. */
  readonly attachable: boolean;
  readonly relationship: TruthEvidenceRelationship;
  readonly quote: string | null;
  readonly sourceLocator: string;
  readonly sourceTitle: string | null;
  readonly trustClass: string | null;
  readonly integrityState: string | null;
  readonly capture: {
    readonly textTruncated: boolean;
    readonly capturedTextBytes: number;
    readonly extractedTextBytes: number;
    readonly capturedTextSha256: string;
    readonly fullExtractedTextSha256: string;
    readonly maximumCapturedTextBytes: number;
  } | null;
  readonly rationale: string | null;
}

/** One server-reported search boundary. The client never infers coverage. */
export interface TruthAnalysisSourceCoverage {
  readonly source: string;
  readonly status:
    | "supplied"
    | "searched"
    | "partial"
    | "not_searched"
    | "unavailable"
    | "failed";
  readonly detail: string | null;
  readonly externalEgress: boolean | null;
}

export interface TruthAnalysisCandidate {
  readonly candidateId: string;
  readonly canonicalSha256: string;
  readonly status: TruthAnalysisCandidateStatus;
  readonly decision:
    | "save_as_proposed"
    | "connect_existing"
    | "dismiss"
    | null;
  readonly proposition: string;
  readonly claimKind: string;
  /** Confidence that the passage expresses this claim, not that it is true. */
  readonly confidenceExtraction: number | null;
  readonly expression: {
    readonly role: TruthExpressionRole;
    readonly quote: string;
    readonly selector: TruthQuoteSelector;
  };
  readonly existingClaimMatch: TruthAnalysisExistingClaimMatch | null;
  readonly evidence: readonly TruthAnalysisEvidenceCandidate[];
  readonly sourceCoverage: readonly TruthAnalysisSourceCoverage[];
  readonly limitations: readonly string[];
}

export interface TruthAnalysisRun {
  readonly schema: string;
  readonly analysisRunId: string;
  readonly storeId: string;
  readonly documentId: string;
  readonly status: TruthAnalysisRunStatus;
  readonly targetChoice: TruthAnalysisTargetChoice;
  readonly targetLabel: string;
  readonly capturedAt: string;
  readonly structuredHeadSha256: string;
  readonly projectionSha256: string;
  readonly execution: TruthAnalysisExecutionSelection;
  readonly candidates: readonly TruthAnalysisCandidate[];
  readonly sourceCoverage: readonly TruthAnalysisSourceCoverage[];
  readonly limitations: readonly string[];
  readonly error: string | null;
  readonly createdAt: string;
  readonly finishedAt: string | null;
}

export interface TruthStartAnalysisRequest {
  readonly targetChoice: TruthAnalysisTargetChoice;
  /** Full frozen capture; the transport never rebuilds it from visible text. */
  readonly capture: CoworkCapturedActionSnapshot;
  readonly execution: TruthAnalysisExecutionSelection;
}

export interface TruthAnalysisCandidateEdits {
  readonly proposition: string;
  readonly claimKind: string;
  readonly expressionRole: TruthExpressionRole;
  readonly evidenceCandidateIds: readonly string[];
}

export interface TruthAnalysisCandidateDecisionRequest {
  readonly analysisRunId: string;
  readonly candidateId: string;
  readonly decision:
    | "save_as_proposed"
    | "connect_existing"
    | "dismiss";
  readonly expectedCanonicalSha256: string;
  readonly existingClaimId?: string;
  readonly edits?: TruthAnalysisCandidateEdits;
}

export interface TruthAnalysisCandidateDecisionReceipt {
  readonly ok: boolean;
  readonly analysisRunId: string;
  readonly candidateId: string;
  readonly candidateStatus: TruthAnalysisCandidateStatus;
  readonly claimId: string | null;
  readonly expressionId: string | null;
}

export interface TruthAnalysisCostControl {
  readonly enforcementClass: "hard_ceiling" | "unavailable";
  readonly ceilingUsdPerWorkerSession: number | null;
  readonly basis: string;
}

export interface TruthAnalysisProviderCapability {
  readonly providerId: string;
  readonly analysisAvailable: boolean;
  readonly unavailableReason: string | null;
  readonly appliesToAllModels: boolean;
  readonly costControl: TruthAnalysisCostControl;
}

/** Server-attested execution eligibility; the client treats absence as unsafe. */
export interface TruthAnalysisCapabilities {
  readonly schema: "wb.cowork.truth-analysis-capabilities/v1";
  readonly requiredCostControl: {
    readonly enforcementClass: "hard_ceiling";
    readonly scope: "worker_model_session";
    readonly maximumUsdPerModelSession: number;
  };
  readonly researchCostControl: {
    readonly enforcementClass: "unavailable";
    readonly scope: "web_search_and_fetch";
    readonly ceilingUsd: null;
    readonly basis: string;
  };
  readonly providers: readonly TruthAnalysisProviderCapability[];
}

/**
 * Operational AI-analysis seam kept separate from the authoritative ledger
 * provider. Losing this provider must never make Truth observation fail.
 */
export interface TruthAnalysisProvider {
  loadCapabilities(): Promise<TruthAnalysisCapabilities>;
  loadCurrent(): Promise<TruthAnalysisRun | null>;
  loadRun(analysisRunId: string): Promise<TruthAnalysisRun>;
  start(request: TruthStartAnalysisRequest): Promise<TruthAnalysisRun>;
  decideCandidate(
    request: TruthAnalysisCandidateDecisionRequest,
  ): Promise<TruthAnalysisCandidateDecisionReceipt>;
  subscribe(listener: TruthInvalidationListener): TruthUnsubscribe;
}

export type TruthInvalidationListener = () => void;
export type TruthUnsubscribe = () => void;

/** Transport-agnostic read and mutation seam consumed by TruthPanel. */
export interface TruthRailProvider {
  load(query: TruthQuery): Promise<TruthClaimsSnapshot>;
  loadClaim(claimId: string): Promise<TruthClaimDetail>;
  subscribe(listener: TruthInvalidationListener): TruthUnsubscribe;
  proposeClaim(request: TruthProposeClaimRequest): Promise<TruthMutationReceipt>;
  connectClaim(request: TruthConnectClaimRequest): Promise<TruthMutationReceipt>;
  decideClaim(request: TruthClaimDecisionRequest): Promise<TruthMutationReceipt>;
}

/** Parent/editor integration kept deliberately free of Tiptap and Yjs types. */
export interface TruthEditorIntegration {
  /** Capture and freeze the current non-empty selection. */
  captureSelection(): Promise<TruthSelectionCapture>;
  /** Capture one full immutable action snapshot for AI-assisted analysis. */
  captureAnalysisTarget?(
    target: TruthAnalysisTargetChoice,
  ): Promise<CoworkCapturedActionSnapshot>;
  /** One-shot present-user navigation; passive selection must never call it. */
  revealPassage(connection: TruthPassageConnection): void;
  /** Optional persistent emphasis without scrolling. */
  focusClaim?(claimId: string | null): void;
  /** View-only focus for a staged candidate that is not a ledger expression yet. */
  focusAnalysisPassage?(target: TruthAnalysisPassageTarget | null): void;
  /** One-shot explicit navigation to a staged candidate's exact expression. */
  revealAnalysisPassage?(target: TruthAnalysisPassageTarget): void;
}

export interface TruthAnalysisPassageTarget {
  readonly candidateId: string;
  readonly selector: TruthQuoteSelector;
}

export interface TruthScrollIntegration {
  readonly scrollContainerRef?: RefCallback<HTMLElement>;
  /** Flush persistence before a scope/filter/tab geometry change. */
  readonly onScrollContainerWillDetach?: () => void;
}

export const isTruthFact = (claim: TruthClaimSummary): boolean =>
  claim.isFact;

export const truthClaimMatchesFilter = (
  claim: TruthClaimSummary,
  filter: TruthClaimFilter,
): boolean => {
  switch (filter) {
    case "all":
      return true;
    case "facts":
      return isTruthFact(claim);
    case "proposed":
      return claim.baseStatus === "proposed";
    case "needs_review":
      return claim.needsReview;
    case "challenged":
      return claim.baseStatus === "challenged";
    case "unconnected":
      return claim.connectionCount === 0;
  }
};
