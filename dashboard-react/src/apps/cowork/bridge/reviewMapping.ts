/**
 * The pure R2-to-rail mapper. It is the single source of truth translation the bridge runs
 * once per pull, producing BOTH the rail's ReviewRailData (the margin cards) and the
 * suggestion adapter's ProposalInput list (the editor marks) from the SAME R2 payload, so
 * cards and marks can never disagree (the one-source-of-truth rule). No HTTP, no DOM, no
 * React: a pure function over the frozen R2 shape (section 1.3).
 *
 * The rail carries three display-only fields R2 does not: `changeType` (derived from the
 * quote and replacement so the card can label and colour itself), `anchorLabel` (a short
 * quote snippet used as the scroll-to affordance), and `documentOrder` (the pull order, a
 * monotonic stand-in the rail sorts by). None of the three is ledger truth, so deriving
 * them here keeps the wire payload minimal.
 */

import type {
  ProposalChangeType,
  ProposalClaimRef,
  ProposalProducer,
  ProvenanceSpan,
  RailDriftHealth,
  ReviewExpression,
  ReviewProposal,
  ReviewRailData,
  TrustState,
} from "../rail/contracts";
import type { EpistemicState, ProposalInput } from "../suggestions/types";
import type {
  R2ClaimRef,
  R2DocPayload,
  R2Expression,
  R2Producer,
  R2Proposal,
  R2ProvenanceSpan,
} from "./types";

/** The two projections one pull yields: the rail cards and the editor ingestion inputs. */
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
  claimRef: expression.claim_ref,
  claimStatus: expression.claim_status,
  claimKind: expression.claim_kind,
});

const mapProvenanceSpan = (span: R2ProvenanceSpan): ProvenanceSpan => ({
  spanId: span.span_id,
  quote: span.quote,
  trustState: span.trust_state as TrustState,
  producer: span.producer === null ? null : mapProducer(span.producer),
  approvalGestureId: span.approval_gesture_id,
});

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

/** Map one R2 proposal to the suggestion adapter's ingestion input. */
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
 * and the ingestion inputs together, so the card set and the ingested proposal set are
 * derived from one array in one pass. The claims tab stays empty here: R2 carries the
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
