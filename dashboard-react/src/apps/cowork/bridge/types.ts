/**
 * The R2 doc-open wire shapes (C1 surface section 1.3), as the dashboard service returns
 * them over `GET /api/truth/doc/<document_id>?store_id=`. These mirror the frozen payload
 * field-for-field in snake_case, so the pure mapper in reviewMapping.ts can translate them
 * into the rail's JSON-compatible ReviewRailData plus the shared proposal-catalog shape
 * without any HTTP knowledge. The wire names win here (the field-name alias table, section
 * 1.0b): `base_doc_sha256` and `model_source` are the surface spellings.
 */

import type {
  CoworkActionTargetChoice,
  CoworkCanonicalTargetSelector,
  CoworkDocumentTargetReference,
} from "../targets";

/** Web-Annotation quote anchor, resolved client-side by anchors.py, never by node id. */
export interface R2QuoteAnchor {
  readonly exact: string;
  readonly prefix: string;
  readonly suffix: string;
}

/** Producing run identity that survives acceptance (I11 provenance). */
export interface R2Producer {
  readonly model: string;
  readonly model_source: string;
  readonly session_id: string;
  readonly surface: string;
}

/** One claim reference on a proposal (S7 one shape everywhere). */
export interface R2ClaimRef {
  readonly claim: string;
  readonly role?: "quote" | "paraphrase" | "summary" | "instantiation";
}

export interface R2ProposalApplicability {
  readonly status: "applicable" | "target_changed" | "unknown";
  readonly reason: string;
  readonly resolved_start?: number;
  readonly resolved_end?: number;
  readonly current_projection_sha256?: string;
  readonly current_structured_head_sha256?: string;
}

/** One open proposal as delivered by R2 doc-get. */
export interface R2Proposal {
  readonly proposal_id: string;
  readonly kind: "edit" | "flag";
  readonly quote_anchor: R2QuoteAnchor;
  readonly replacement: string | null;
  readonly rationale: string;
  readonly tldr: string;
  readonly producer: R2Producer;
  readonly epistemic_state: "ai_proposed";
  readonly base_doc_sha256: string;
  readonly canonical_sha256: string;
  /** Target-level placement proof; base_ok remains a rolling compatibility alias. */
  readonly applicability?: R2ProposalApplicability;
  readonly base_ok: boolean;
  readonly status: "open" | "redraft_pending";
  readonly fixes_ref: string | null;
  readonly claim_refs?: readonly R2ClaimRef[];
  readonly created_at: string;
}

/** One expression row (the claim underneath a passage, read path). */
export interface R2Expression {
  readonly expression_id: string;
  readonly span_id: string;
  readonly node_id_hint: string | null;
  readonly quote: string;
  readonly quote_anchor?: R2QuoteAnchor;
  readonly claim_ref: string;
  readonly claim_status:
    | "confirmed"
    | "needs_review"
    | "proposed"
    | "challenged"
    | "rejected"
    | "superseded"
    | "retracted"
    | "expired"
    | null;
  readonly claim_kind: string | null;
}

/** One provenance span for the inspector, re-anchored by quote (I12). */
export interface R2ProvenanceSpan {
  readonly span_id: string;
  readonly quote: string;
  readonly quote_anchor?: R2QuoteAnchor;
  readonly trust_state: "human" | "ai_confirmed" | "ai_proposed";
  readonly producer: R2Producer | null;
  readonly approval_gesture_id: string | null;
}

/** Rich provenance is parsed from unknown at the bridge boundary. */
export type R2ProvenanceView = Readonly<Record<string, unknown>>;

/** The R2 hashes block (section 1.3). */
export interface R2Hashes {
  readonly ydoc_snapshot_sha256: string | null;
  readonly last_materialized_sha256: string | null;
  readonly current_file_sha256: string | null;
}

/** The R2 drift block (section 1.3). */
export interface R2Drift {
  readonly state: "clean" | "drifted" | "missing";
  readonly diff_available: boolean;
}

export interface R2CoworkVerifyCapability {
  readonly enabled: boolean;
  readonly contract_version: number;
  readonly can_run: boolean;
  readonly can_configure: boolean;
  readonly can_cothink: boolean;
  readonly disabled_reason: string | null;
}

export interface R2VerifyActor {
  readonly kind: string;
  readonly ref: string | null;
  readonly meta: Readonly<Record<string, unknown>> | null;
}

export interface R2VerificationCheck {
  readonly id: string;
  readonly stable_key: string;
  readonly version: number;
  readonly title: string;
  readonly method: {
    readonly mechanism: string;
    readonly executor_ref: string;
  };
  readonly limitations: readonly string[];
  readonly origin: {
    readonly definition_origin: string;
    readonly author: R2VerifyActor;
  };
  readonly data_sharing: {
    readonly class: string;
    readonly external_egress: boolean | null;
    readonly basis: string;
  };
  readonly availability: {
    readonly state: "available" | "unavailable";
    readonly reason: string | null;
    readonly execution_location: string | null;
  };
  readonly binding: {
    readonly id: string;
    readonly selected: boolean;
    readonly configuration: Readonly<Record<string, unknown>>;
  };
}

export interface R2VerificationCriterion {
  readonly id: string;
  readonly stable_key: string;
  readonly version: number;
  readonly title: string;
  readonly description: string;
  readonly kind: string;
  readonly author_origin: {
    readonly definition_origin: string;
    readonly author: R2VerifyActor;
  };
  readonly effective_activation: {
    readonly id: string | null;
    readonly enabled: boolean;
    readonly required: boolean;
    readonly locked: boolean;
    readonly scope: Readonly<Record<string, unknown>> | null;
    readonly origin: string | null;
    readonly criterion_check_binding_id: string | null;
    readonly selected_check_available: boolean;
    readonly authorized_by: R2VerifyActor | null;
  };
  readonly mechanism_availability: {
    readonly state: "available" | "unavailable";
    readonly available_check_count: number;
    readonly total_check_count: number;
  };
  readonly operational_state:
    | "active"
    | "inactive"
    | "unavailable"
    | "blocked_required_check";
  readonly checks: readonly R2VerificationCheck[];
  readonly issues: readonly Readonly<Record<string, unknown>>[];
}

export type R2VerifyCostEnforcementClass =
  | "hard_ceiling"
  | "estimate"
  | "unavailable";

export interface R2VerifyProviderCostControl {
  readonly provider_id: string;
  readonly enforcement_class: R2VerifyCostEnforcementClass;
  readonly ceiling_usd_per_worker_session: number | null;
  readonly basis: string;
}

export interface R2VerifyExecutionPlan {
  readonly schema: string;
  readonly authoritative: true;
  readonly checker: {
    readonly execution_class: "in_process";
    readonly mechanism: "deterministic_exact_match";
    readonly model_call: false;
    readonly external_egress: false;
    readonly content_boundary: "captured_target";
  };
  readonly coordination: {
    readonly execution_class: "account_backed_agent";
    readonly selection: {
      readonly mode: "explicit_at_run_start";
      readonly provider_id: string | null;
      readonly model_id: string | null;
      readonly provider_label: string | null;
      readonly model_label: string | null;
    };
    readonly content_boundary: "entire_frozen_document";
    readonly external_egress: true;
    readonly fallback: {
      readonly provider_model_fallback: false;
      readonly failure_mode: "fail_closed";
    };
    readonly worker_sessions: {
      readonly initial: number;
      readonly maximum: number;
      readonly conditional_roles: readonly string[];
    };
    readonly cost_control: R2VerifyProviderCostControl | null;
    readonly provider_cost_controls: readonly R2VerifyProviderCostControl[];
  };
}

export interface R2VerificationConfiguration {
  readonly schema: string;
  readonly document_id: string;
  readonly execution_plan?: R2VerifyExecutionPlan;
  readonly coordination: {
    readonly deprecated?: boolean;
    readonly authoritative_projection?: string;
    readonly required: boolean;
    readonly selection: string;
    readonly content_boundary: string;
    readonly egress_class: string;
    readonly external_egress: boolean;
    readonly cost_ceiling_usd_per_worker: number;
    readonly cost_ceiling_semantics?: string;
    readonly separate_reviser_for_findings: boolean;
    readonly pattern?: string;
    readonly base_worker_calls?: number;
    readonly maximum_worker_calls?: number;
  };
  readonly criteria: readonly R2VerificationCriterion[];
}

export interface R2EvaluationRunSummary {
  readonly run_id: string;
  readonly status:
    | "prepared"
    | "queued"
    | "running"
    | "completed"
    | "completed_with_failures"
    | "failed"
    | "cancelled";
  readonly purpose: string;
  readonly target_label: string;
  readonly coverage_label: string;
  readonly current_version: boolean;
  readonly result_count: number;
  readonly surfaced_result_count: number;
  readonly coordination_status: "pending" | "completed" | "unavailable";
  readonly provider_label: string | null;
  readonly provider_id?: string | null;
  readonly model_label: string | null;
  readonly model_id?: string | null;
  readonly created_at: string;
  readonly finished_at: string | null;
}

export interface R2EvaluationResult {
  readonly result_id: string;
  readonly run_id: string;
  readonly kind:
    | "conforming"
    | "nonconforming"
    | "inconclusive"
    | "review_comment";
  readonly criterion_label: string;
  readonly criterion_statement: string;
  readonly check_label: string;
  readonly method_label: string;
  readonly explanation: string;
  readonly quote_anchor: R2QuoteAnchor | null;
  readonly coverage_label: string;
  readonly limitations: readonly string[];
  readonly current_version: boolean;
  readonly disposition:
    | "surface_result"
    | "retain_without_interrupting"
    | "surface_proposal"
    | "suggest_cothink"
    | "defer_until_boundary"
    | "escalate";
  readonly canonical_sha256: string;
  readonly proposal_ids: readonly string[];
  readonly created_at: string;
}

export interface R2CothinkItem {
  readonly item_id: string;
  readonly subtype: "alternative_perspective";
  readonly content: string;
  readonly rationale: string;
  readonly target_label: string;
  readonly quote_anchor: R2QuoteAnchor | null;
  readonly status: "open" | "parked" | "dismissed";
  readonly current_version: boolean;
  readonly canonical_sha256: string;
  readonly created_at: string;
}

export interface R2CothinkOutcome {
  readonly outcome_id: string;
  readonly status:
    | "running"
    | "completed_with_item"
    | "completed_no_useful_item"
    | "unavailable";
  readonly rationale: string;
  readonly target_label: string;
  readonly current_version: boolean;
  readonly provider_id: string;
  readonly model_id: string;
  readonly created_at: string;
  readonly finished_at: string | null;
}

export interface R2VerificationRecheckIntent {
  readonly id: string;
  readonly sitting_id: string;
  readonly document_id: string;
  readonly source_run_id: string;
  readonly proposal_ids: readonly string[];
  readonly pending_proposal_ids: readonly string[];
  readonly fulfilled_by_run_ids: readonly string[];
  readonly committed_at: string;
  readonly user_goal: string;
  readonly protected_intent: string;
  readonly status:
    | "pending_capture"
    | "user_action_required"
    | "fulfilled";
  readonly original_action_target: {
    readonly action_snapshot_id: string;
    readonly source: CoworkActionTargetChoice | null;
    readonly label: string | null;
    readonly kind: "document" | "text_quote";
    readonly selector: CoworkCanonicalTargetSelector;
    readonly target_text_sha256: string;
    readonly target_reference: CoworkDocumentTargetReference | null;
    readonly target_reference_sha256: string | null;
  };
  readonly execution: {
    readonly provider_id: string;
    readonly model_id: string;
    readonly provider_label: string;
    readonly model_label: string;
  };
  readonly requires: {
    readonly fresh_action_snapshot: boolean;
    readonly fresh_model_call_authorization: boolean;
    readonly same_target_source: boolean;
    readonly same_target_reference: boolean;
    readonly exact_target_resolution: boolean;
    readonly user_affirmed_exact_target_required: boolean;
    readonly on_unresolved: "user_action_required";
    readonly allow_widen_to_whole_document: false;
  };
}

/** The full R2 doc-open payload (section 1.3). */
export interface R2DocPayload {
  readonly document_id: string;
  readonly store_id: string;
  readonly path: string;
  readonly title: string;
  readonly profile: string;
  readonly hashes: R2Hashes;
  readonly drift: R2Drift;
  /** Additive capability handshake. Older servers may omit these fields. */
  readonly capabilities?: {
    readonly cowork_verify?: R2CoworkVerifyCapability;
  };
  readonly evaluation_run_summaries?: readonly R2EvaluationRunSummary[];
  readonly evaluation_results?: readonly R2EvaluationResult[];
  readonly verification_recheck_intents?: readonly R2VerificationRecheckIntent[];
  readonly cothink_items?: readonly R2CothinkItem[];
  readonly cothink_outcomes?: readonly R2CothinkOutcome[];
  readonly verification_configuration?: R2VerificationConfiguration;
  readonly open_proposals: readonly R2Proposal[];
  readonly expressions: readonly R2Expression[];
  readonly provenance_spans: readonly R2ProvenanceSpan[];
  /** Additive v1 projection. The mapper validates every field before use. */
  readonly provenance?: R2ProvenanceView;
  readonly events_cursor: string;
}
