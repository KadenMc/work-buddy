/**
 * The R2 doc-open wire shapes (C1 surface section 1.3), as the dashboard service returns
 * them over `GET /api/truth/doc/<document_id>?store_id=`. These mirror the frozen payload
 * field-for-field in snake_case, so the pure mapper in reviewMapping.ts can translate them
 * into the rail's JSON-compatible ReviewRailData plus the suggestion adapter's ProposalInput
 * without any HTTP knowledge. The wire names win here (the field-name alias table, section
 * 1.0b): `base_doc_sha256` and `model_source` are the surface spellings.
 */

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
  readonly claim_ref: string;
  readonly claim_status:
    | "confirmed"
    | "needs_review"
    | "proposed"
    | "rejected"
    | null;
  readonly claim_kind: string | null;
}

/** One provenance span for the inspector, re-anchored by quote (I12). */
export interface R2ProvenanceSpan {
  readonly span_id: string;
  readonly quote: string;
  readonly trust_state: "human" | "ai_confirmed" | "ai_proposed";
  readonly producer: R2Producer | null;
  readonly approval_gesture_id: string | null;
}

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

/** The full R2 doc-open payload (section 1.3). */
export interface R2DocPayload {
  readonly document_id: string;
  readonly store_id: string;
  readonly path: string;
  readonly title: string;
  readonly profile: string;
  readonly hashes: R2Hashes;
  readonly drift: R2Drift;
  readonly open_proposals: readonly R2Proposal[];
  readonly expressions: readonly R2Expression[];
  readonly provenance_spans: readonly R2ProvenanceSpan[];
  readonly events_cursor: string;
}
