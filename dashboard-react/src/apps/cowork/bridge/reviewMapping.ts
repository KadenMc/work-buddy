/**
 * The pure R2-to-rail mapper. It is the single source of truth translation the bridge runs
 * once per pull, producing BOTH the rail's ReviewRailData and the ProposalInput catalog
 * used by view-only editor decorations and isolated sitting materialization. Both derive
 * from the SAME R2 payload, so review cards and editor annotations cannot disagree. No
 * HTTP, DOM, or React: this is a pure function over the frozen R2 shape.
 *
 * The rail carries three display-only fields R2 does not: `changeType` (derived from the
 * quote and replacement so the card can label and colour itself), `anchorLabel` (a short
 * quote snippet used as the scroll-to affordance), and `documentOrder` (the pull order, a
 * monotonic stand-in the rail sorts by). None of the three is ledger truth, so deriving
 * them here keeps the wire payload minimal.
 */

import type {
  CothinkItem,
  CothinkOutcome,
  EvaluationResult,
  EvaluationRunSummary,
  ProposalChangeType,
  ProposalClaimRef,
  ProposalProducer,
  ProvenanceSpan,
  RailDriftHealth,
  ReviewExpression,
  ReviewProposal,
  ReviewRailData,
  TrustState,
  VerificationConfiguration,
  VerificationRecheckIntent,
  VerifyProviderCostControl,
} from "../rail/contracts";
import type { EpistemicState, ProposalInput } from "../suggestions/types";
import type {
  R2ClaimRef,
  R2CothinkItem,
  R2CothinkOutcome,
  R2DocPayload,
  R2EvaluationResult,
  R2EvaluationRunSummary,
  R2Expression,
  R2Producer,
  R2Proposal,
  R2ProvenanceSpan,
  R2VerificationConfiguration,
  R2VerificationCriterion,
  R2VerifyProviderCostControl,
  R2VerificationRecheckIntent,
} from "./types";

/** The two projections one pull yields: rail cards and the authoritative proposal catalog. */
export interface MappedReview {
  readonly railData: ReviewRailData;
  readonly proposalInputs: readonly ProposalInput[];
}

const DOCUMENT_ORDER_STEP = 10;
const ANCHOR_LABEL_MAX = 32;

/**
 * Classify an edit proposal for the card's kind label. A cleared replacement is a deletion,
 * a replacement that still contains the exact quote is an insertion (text added around the
 * quote), and anything else is a modification. Flags carry no change type.
 */
export const deriveChangeType = (
  proposal: R2Proposal,
): ProposalChangeType | undefined => {
  if (proposal.kind === "flag" || proposal.replacement === null) return undefined;
  if (proposal.replacement.length === 0) return "deletion";
  if (
    proposal.quote_anchor.exact.length > 0 &&
    proposal.replacement.includes(proposal.quote_anchor.exact)
  ) {
    return "insertion";
  }
  return "modification";
};

/** A short, single-line snippet of the quote for the card's scroll-to affordance. */
export const deriveAnchorLabel = (proposal: R2Proposal): string => {
  const quote = proposal.quote_anchor.exact.replace(/\s+/gu, " ").trim();
  if (quote.length === 0) return "this passage";
  if (quote.length <= ANCHOR_LABEL_MAX) return `"${quote}"`;
  return `"${quote.slice(0, ANCHOR_LABEL_MAX - 1).trimEnd()}…"`;
};

const mapProducer = (producer: R2Producer): ProposalProducer => ({
  model: producer.model,
  modelSource: producer.model_source,
  sessionId: producer.session_id,
  surface: producer.surface,
});

const mapClaimRef = (ref: R2ClaimRef): ProposalClaimRef => ({
  claim: ref.claim,
  role: ref.role ?? "instantiation",
});

const mapExpression = (expression: R2Expression): ReviewExpression => ({
  expressionId: expression.expression_id,
  spanId: expression.span_id,
  nodeIdHint: expression.node_id_hint,
  quote: expression.quote,
  quoteAnchor:
    expression.quote_anchor ?? {
      exact: expression.quote,
      prefix: "",
      suffix: "",
    },
  claimRef: expression.claim_ref,
  claimStatus: expression.claim_status,
  claimKind: expression.claim_kind,
});

const mapProvenanceSpan = (span: R2ProvenanceSpan): ProvenanceSpan => ({
  spanId: span.span_id,
  quote: span.quote,
  quoteAnchor:
    span.quote_anchor ?? {
      exact: span.quote,
      prefix: "",
      suffix: "",
    },
  trustState: span.trust_state as TrustState,
  producer: span.producer === null ? null : mapProducer(span.producer),
  approvalGestureId: span.approval_gesture_id,
});

const mapEvaluationRun = (
  run: R2EvaluationRunSummary,
): EvaluationRunSummary => ({
  runId: run.run_id,
  status: run.status,
  purpose: run.purpose,
  targetLabel: run.target_label,
  coverageLabel: run.coverage_label,
  currentVersion: run.current_version,
  resultCount: run.result_count,
  surfacedResultCount: run.surfaced_result_count,
  coordinationStatus: run.coordination_status,
  providerLabel: run.provider_label,
  providerId: run.provider_id ?? null,
  modelLabel: run.model_label,
  modelId: run.model_id ?? null,
  createdAt: run.created_at,
  finishedAt: run.finished_at,
});

const mapEvaluationResult = (
  result: R2EvaluationResult,
): EvaluationResult => ({
  resultId: result.result_id,
  runId: result.run_id,
  kind: result.kind,
  criterionLabel: result.criterion_label,
  criterionStatement: result.criterion_statement,
  checkLabel: result.check_label,
  methodLabel: result.method_label,
  explanation: result.explanation,
  quoteAnchor: result.quote_anchor,
  coverageLabel: result.coverage_label,
  limitations: result.limitations,
  currentVersion: result.current_version,
  disposition: result.disposition,
  canonicalSha256: result.canonical_sha256,
  proposalIds: result.proposal_ids,
  createdAt: result.created_at,
});

const mapCothinkItem = (item: R2CothinkItem): CothinkItem => ({
  itemId: item.item_id,
  subtype: item.subtype,
  content: item.content,
  rationale: item.rationale,
  targetLabel: item.target_label,
  quoteAnchor: item.quote_anchor,
  status: item.status,
  currentVersion: item.current_version,
  canonicalSha256: item.canonical_sha256,
  createdAt: item.created_at,
});

const mapCothinkOutcome = (
  outcome: R2CothinkOutcome,
): CothinkOutcome => ({
  outcomeId: outcome.outcome_id,
  status: outcome.status,
  rationale: outcome.rationale,
  targetLabel: outcome.target_label,
  currentVersion: outcome.current_version,
  providerId: outcome.provider_id,
  modelId: outcome.model_id,
  createdAt: outcome.created_at,
  finishedAt: outcome.finished_at,
});

const mapVerificationRecheckIntent = (
  intent: R2VerificationRecheckIntent,
): VerificationRecheckIntent => ({
  intentId: intent.id,
  sittingId: intent.sitting_id,
  sourceRunId: intent.source_run_id,
  proposalIds: intent.proposal_ids,
  pendingProposalIds: intent.pending_proposal_ids,
  fulfilledByRunIds: intent.fulfilled_by_run_ids,
  committedAt: intent.committed_at,
  status: intent.status,
  userGoal: intent.user_goal,
  protectedIntent: intent.protected_intent,
  originalActionTarget: {
    actionSnapshotId: intent.original_action_target.action_snapshot_id,
    source: intent.original_action_target.source,
    label: intent.original_action_target.label,
    kind: intent.original_action_target.kind,
    selector: intent.original_action_target.selector,
    targetTextSha256: intent.original_action_target.target_text_sha256,
    targetReference: intent.original_action_target.target_reference,
    targetReferenceSha256:
      intent.original_action_target.target_reference_sha256,
  },
  execution: {
    providerId: intent.execution.provider_id,
    modelId: intent.execution.model_id,
    providerLabel: intent.execution.provider_label,
    modelLabel: intent.execution.model_label,
  },
  requires: {
    freshActionSnapshot: intent.requires.fresh_action_snapshot,
    freshModelCallAuthorization:
      intent.requires.fresh_model_call_authorization,
    sameTargetSource: intent.requires.same_target_source,
    sameTargetReference: intent.requires.same_target_reference,
    exactTargetResolution: intent.requires.exact_target_resolution,
    userAffirmedExactTargetRequired:
      intent.requires.user_affirmed_exact_target_required,
    allowWidenToWholeDocument:
      intent.requires.allow_widen_to_whole_document,
  },
});

const mapVerificationCriterion = (
  criterion: R2VerificationCriterion,
) => ({
  id: criterion.id,
  stableKey: criterion.stable_key,
  version: criterion.version,
  title: criterion.title,
  description: criterion.description,
  kind: criterion.kind,
  definitionOrigin: criterion.author_origin.definition_origin,
  author: criterion.author_origin.author,
  activationId: criterion.effective_activation.id,
  enabled: criterion.effective_activation.enabled,
  required: criterion.effective_activation.required,
  locked: criterion.effective_activation.locked,
  activationOrigin: criterion.effective_activation.origin,
  authorizedBy: criterion.effective_activation.authorized_by,
  operationalState: criterion.operational_state,
  availableCheckCount:
    criterion.mechanism_availability.available_check_count,
  totalCheckCount: criterion.mechanism_availability.total_check_count,
  checks: criterion.checks.map((check) => ({
    id: check.id,
    stableKey: check.stable_key,
    version: check.version,
    title: check.title,
    mechanism: check.method.mechanism,
    executorRef: check.method.executor_ref,
    limitations: check.limitations,
    definitionOrigin: check.origin.definition_origin,
    author: check.origin.author,
    dataSharingClass: check.data_sharing.class,
    externalEgress: check.data_sharing.external_egress,
    dataSharingBasis: check.data_sharing.basis,
    availability: check.availability.state,
    unavailableReason: check.availability.reason,
    executionLocation: check.availability.execution_location,
    bindingId: check.binding.id,
    selected: check.binding.selected,
    configuration: check.binding.configuration,
  })),
  issues: criterion.issues,
});

const mapVerifyProviderCostControl = (
  control: R2VerifyProviderCostControl,
): VerifyProviderCostControl => ({
  providerId: control.provider_id,
  enforcementClass: control.enforcement_class,
  ceilingUsdPerWorkerSession: control.ceiling_usd_per_worker_session,
  basis: control.basis,
});

export const mapVerifyExecutionPlan = (
  executionPlan: NonNullable<R2VerificationConfiguration["execution_plan"]>,
): NonNullable<VerificationConfiguration["executionPlan"]> => ({
  schema: executionPlan.schema,
  authoritative: executionPlan.authoritative,
  checker: {
    executionClass: executionPlan.checker.execution_class,
    mechanism: executionPlan.checker.mechanism,
    modelCall: executionPlan.checker.model_call,
    externalEgress: executionPlan.checker.external_egress,
    contentBoundary: executionPlan.checker.content_boundary,
  },
  coordination: {
    executionClass: executionPlan.coordination.execution_class,
    selection: {
      mode: executionPlan.coordination.selection.mode,
      providerId: executionPlan.coordination.selection.provider_id,
      modelId: executionPlan.coordination.selection.model_id,
      providerLabel: executionPlan.coordination.selection.provider_label,
      modelLabel: executionPlan.coordination.selection.model_label,
    },
    contentBoundary: executionPlan.coordination.content_boundary,
    externalEgress: executionPlan.coordination.external_egress,
    fallback: {
      providerModelFallback:
        executionPlan.coordination.fallback.provider_model_fallback,
      failureMode: executionPlan.coordination.fallback.failure_mode,
    },
    workerSessions: {
      initial: executionPlan.coordination.worker_sessions.initial,
      maximum: executionPlan.coordination.worker_sessions.maximum,
      conditionalRoles:
        executionPlan.coordination.worker_sessions.conditional_roles,
    },
    costControl:
      executionPlan.coordination.cost_control === null
        ? null
        : mapVerifyProviderCostControl(
            executionPlan.coordination.cost_control,
          ),
    providerCostControls:
      executionPlan.coordination.provider_cost_controls.map(
        mapVerifyProviderCostControl,
      ),
  },
});

const mapVerificationConfiguration = (
  payload: R2DocPayload,
): VerificationConfiguration => {
  const configuration: R2VerificationConfiguration | undefined =
    payload.verification_configuration;
  const executionPlan = configuration?.execution_plan;
  return {
    schema:
      configuration?.schema ??
      "work-buddy.cowork-verify-configuration/unavailable",
    documentId: configuration?.document_id ?? payload.document_id,
    executionPlan:
      executionPlan === undefined
        ? null
        : mapVerifyExecutionPlan(executionPlan),
    coordination:
      configuration === undefined
        ? null
        : {
            deprecated: configuration.coordination.deprecated ?? true,
            authoritativeProjection:
              configuration.coordination.authoritative_projection ??
              "execution_plan",
            required: configuration.coordination.required,
            selection: configuration.coordination.selection,
            contentBoundary: configuration.coordination.content_boundary,
            egressClass: configuration.coordination.egress_class,
            externalEgress: configuration.coordination.external_egress,
            costCeilingUsdPerWorker:
              configuration.coordination.cost_ceiling_usd_per_worker,
            costCeilingSemantics:
              configuration.coordination.cost_ceiling_semantics ??
              "requested_launch_budget_not_provider_guarantee",
            separateReviserForFindings:
              configuration.coordination.separate_reviser_for_findings,
            pattern:
              configuration.coordination.pattern ??
              "coordinator_then_optional_reviser_then_coordinator",
            baseWorkerCalls:
              configuration.coordination.base_worker_calls ?? 1,
            maximumWorkerCalls:
              configuration.coordination.maximum_worker_calls ?? 3,
          },
    criteria:
      configuration?.criteria.map(mapVerificationCriterion) ?? [],
  };
};

/** Map one R2 proposal to the rail card shape, adding the three display-only fields. */
export const mapProposal = (
  proposal: R2Proposal,
  index: number,
): ReviewProposal => {
  const changeType = deriveChangeType(proposal);
  return {
    proposalId: proposal.proposal_id,
    kind: proposal.kind,
    ...(changeType === undefined ? {} : { changeType }),
    quoteAnchor: proposal.quote_anchor,
    replacement: proposal.replacement,
    rationale: proposal.rationale,
    tldr: proposal.tldr,
    producer: mapProducer(proposal.producer),
    epistemicState: proposal.epistemic_state,
    baseDocSha256: proposal.base_doc_sha256,
    canonicalSha256: proposal.canonical_sha256,
    baseOk: proposal.base_ok,
    status: proposal.status,
    fixesRef: proposal.fixes_ref,
    claimRefs: (proposal.claim_refs ?? []).map(mapClaimRef),
    createdAt: proposal.created_at,
    anchorLabel: deriveAnchorLabel(proposal),
    documentOrder: index * DOCUMENT_ORDER_STEP,
  };
};

/** Map one R2 proposal to the shared decoration and sitting-catalog shape. */
export const mapProposalInput = (proposal: R2Proposal): ProposalInput => ({
  proposal_id: proposal.proposal_id,
  kind: proposal.kind,
  quoteAnchor: proposal.quote_anchor,
  replacement: proposal.replacement,
  attrs: {
    proposal_id: proposal.proposal_id,
    producer: proposal.producer.session_id || proposal.producer.model,
    epistemic: proposal.epistemic_state as EpistemicState,
  },
  base_doc_sha256: proposal.base_doc_sha256,
  canonical_sha256: proposal.canonical_sha256,
});

const mapDrift = (payload: R2DocPayload): RailDriftHealth => {
  const proposals = payload.open_proposals;
  return {
    state: payload.drift.state,
    openProposalCount: proposals.length,
    openFlagCount: proposals.filter((item) => item.kind === "flag").length,
    lastMaterializedSha256: payload.hashes.last_materialized_sha256,
    currentFileSha256: payload.hashes.current_file_sha256,
  };
};

/**
 * The one mapping the bridge runs per pull. It projects the R2 payload into the rail data
 * and proposal catalog together, so the card set, decorations, and sitting inputs derive
 * from one array in one pass. The claims tab stays empty here: R2 carries the
 * expression and provenance read layers but not the full claim-review payloads (proposition,
 * receipts), which ride the kernel claim reads, so a live claims tab is a separate pull the
 * bridge does not perform in v1.
 */
export const mapR2ToReview = (payload: R2DocPayload): MappedReview => {
  const proposals = payload.open_proposals;
  const railData: ReviewRailData = {
    documentId: payload.document_id,
    title: payload.title,
    drift: mapDrift(payload),
    verifyCapability: {
      enabled: payload.capabilities?.cowork_verify?.enabled ?? false,
      contractVersion:
        payload.capabilities?.cowork_verify?.contract_version ?? 0,
      canRun: payload.capabilities?.cowork_verify?.can_run ?? false,
      canConfigure:
        payload.capabilities?.cowork_verify?.can_configure ?? false,
      canCothink: payload.capabilities?.cowork_verify?.can_cothink ?? false,
      disabledReason:
        payload.capabilities?.cowork_verify?.disabled_reason ??
        "Co-work Verify is not available from this server.",
    },
    verificationConfiguration: mapVerificationConfiguration(payload),
    evaluationRuns: (payload.evaluation_run_summaries ?? []).map(
      mapEvaluationRun,
    ),
    evaluationResults: (payload.evaluation_results ?? []).map(
      mapEvaluationResult,
    ),
    verificationRecheckIntents: (
      payload.verification_recheck_intents ?? []
    ).map(mapVerificationRecheckIntent),
    cothinkItems: (payload.cothink_items ?? []).map(mapCothinkItem),
    cothinkOutcomes: (payload.cothink_outcomes ?? []).map(
      mapCothinkOutcome,
    ),
    proposals: proposals.map(mapProposal),
    expressions: payload.expressions.map(mapExpression),
    provenanceSpans: payload.provenance_spans.map(mapProvenanceSpan),
    claims: [],
  };
  return {
    railData,
    proposalInputs: proposals.map(mapProposalInput),
  };
};
