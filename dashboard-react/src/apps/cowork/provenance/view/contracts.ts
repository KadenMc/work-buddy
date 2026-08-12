import type { QuoteAnchor } from "../../rail/contracts";

export type ProvenanceAuthorshipKind = "human" | "ai" | "mixed" | "unknown";
export type ProvenanceReviewStatus = "reviewed" | "not_reviewed" | "not_applicable" | "unknown";
export type ProvenanceCurrentness = "current" | "stale" | "requires_reanchor" | "unavailable";
export type ProvenanceResolution = "resolved" | "conflicted";
export type ProvenanceIdentityStatus =
  | "local_actor_ref"
  | "account_ref"
  | "claimed_name";

export interface ProvenanceActor {
  readonly kind: string;
  readonly ref: string | null;
  readonly meta: Readonly<Record<string, unknown>> | null;
}
export interface ProvenanceContributor {
  readonly kind: "human";
  readonly ref: string | null;
  readonly label: string | null;
  readonly identityStatus: ProvenanceIdentityStatus;
}
export interface ProvenanceAttestation {
  readonly attestationId: string;
  readonly at: string;
  readonly assertedBy: ProvenanceActor;
  readonly scope: {
    readonly kind: "document_version" | "document_span";
    readonly documentVersionId: string | null;
    readonly documentSpanId: string | null;
    readonly structuredHeadSha256: string;
  };
  readonly authorship: { readonly kind: ProvenanceAuthorshipKind; readonly contributors: readonly ProvenanceContributor[] };
  readonly humanReview: { readonly status: ProvenanceReviewStatus; readonly reviewers: readonly ProvenanceContributor[] };
  readonly source: Readonly<Record<string, unknown>>;
  readonly basis: { readonly kind: string; readonly ref: string | null };
  readonly supersedesId: string | null;
  readonly canonicalSha256: string;
}
export interface ProvenanceTarget {
  readonly projectionId: string;
  readonly target: {
    readonly kind: "document_version" | "document_span";
    readonly documentVersionId: string | null;
    readonly documentSpanId: string | null;
    readonly structuredHeadSha256: string;
    readonly currentness: ProvenanceCurrentness;
  };
  readonly span: QuoteAnchor | null;
  readonly effectiveAttestations: readonly ProvenanceAttestation[];
  readonly effectiveAttestation: ProvenanceAttestation | null;
  readonly resolution: ProvenanceResolution;
  readonly reviewEligibility:
    | "eligible"
    | "stale_target"
    | "conflicted"
    | "not_ai_authored"
    | "already_reviewed"
    | "not_applicable";
  readonly issue: { readonly code: string; readonly message: string } | null;
  readonly history: readonly ProvenanceAttestation[];
}
export interface ProvenanceData {
  readonly schema: "cowork-provenance-view/v1";
  readonly currentStructuredHeadSha256: string | null;
  readonly documentDefault: ProvenanceTarget | null;
  readonly spans: readonly ProvenanceTarget[];
  readonly history: readonly ProvenanceAttestation[];
  readonly summary: {
    readonly totalTargets: number;
    readonly currentSpanCount: number;
    readonly aiUnreviewedCount: number;
    readonly reviewedCount: number;
    readonly conflictedCount: number;
    readonly staleCount: number;
    readonly unrecorded: boolean;
  };
}

export type ProvenanceLoad =
  | { readonly state: "ready"; readonly data: ProvenanceData }
  | { readonly state: "unavailable"; readonly reason: string };

export interface ProvenanceTargetResolution {
  readonly state: "unique" | "missing" | "ambiguous";
  readonly documentOrder: number | null;
  /** Exclusive ProseMirror position; present only for a unique quote match. */
  readonly documentEnd: number | null;
}

export interface ProvenanceEditorIntegration {
  resolveTarget(anchor: QuoteAnchor): ProvenanceTargetResolution;
  /** True after an unsynchronized local-human edit invalidates the pulled head. */
  isLocallyDirty(): boolean;
  hasText(): boolean;
  hasUncoveredText(anchors: readonly QuoteAnchor[]): boolean;
  focusTarget(id: string): void;
  revealTarget(id: string): void;
}

export interface ProvenanceProvider {
  load(): Promise<ProvenanceLoad>;
  /** Force one fresh authoritative document pull (used by mutation preflight). */
  refresh(): Promise<ProvenanceLoad>;
  subscribe(listener: () => void): () => void;
  markReviewed(attestationId: string, expectedStructuredHeadSha256: string): Promise<void>;
}

export interface ProvenanceMutationBarrier {
  runWithSynchronizedDocument<Result>(
    operation: (snapshot: {
      readonly structuredHeadSha256: string;
    }) => Promise<Result>,
  ): Promise<Result>;
}
