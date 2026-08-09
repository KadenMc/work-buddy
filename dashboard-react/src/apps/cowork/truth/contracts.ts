import type { RefCallback } from "react";

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
  /** One-shot present-user navigation; passive selection must never call it. */
  revealPassage(connection: TruthPassageConnection): void;
  /** Optional persistent emphasis without scrolling. */
  focusClaim?(claimId: string | null): void;
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
