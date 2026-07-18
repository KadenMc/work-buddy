import type { Editor } from "@tiptap/core";

/**
 * The TypeScript surface for the vendored tracked-change engine, frozen verbatim from
 * C1 surface section 3. The engine is swappable behind this seam, so the adapter type
 * is the stable contract and the suggest-changes 0.1.8 source is the contained blast
 * radius. Zero em-dashes and zero prose semicolons by rule.
 */

/** Ledger-canonical epistemic state for a rendered proposal (PRD section 7 tri-state). */
export type EpistemicState = "ai_proposed" | "ai_confirmed" | "human";

/** A quote anchor as delivered by R2, resolved to positions by the client anchor.ts. */
export interface QuoteAnchor {
  readonly exact: string;
  readonly prefix: string;
  readonly suffix: string;
}

/** Attribution attrs forked onto the insertion/deletion/modification marks (SP-1 delta 2). */
export interface WbSuggestionAttrs {
  /** Suggestion group id == kernel proposal_id (generateId injected). */
  proposal_id: string;
  /** Producing run or session ref (I11 provenance survives acceptance). */
  producer: string;
  epistemic: EpistemicState;
}

/** One ledger proposal as delivered by R2 doc-get, the ingestion input. */
export interface ProposalInput {
  proposal_id: string;
  kind: "edit" | "flag";
  quoteAnchor: QuoteAnchor;
  /** null for a flag. */
  replacement: string | null;
  attrs: WbSuggestionAttrs;
  base_doc_sha256: string;
  canonical_sha256: string;
}

/**
 * The shipped gesture-kind name a human decision carries (S1). UI labels map to these
 * kinds once in surface section 1.5 (Accept to confirm, Amend to edit_confirm, Reject to
 * reject_plain, and so on).
 */
export type SittingVerb =
  | "confirm"
  | "edit_confirm"
  | "reject_plain"
  | "reject_as_false"
  | "reject_as_preference"
  | "redirect"
  | "defer"
  | "endorse"
  | "dismiss";

/** Human decision collected in the editor, submitted as one R5 item. */
export interface DecisionItem {
  proposal_id: string;
  verb: SittingVerb;
  /** Echoes the SHOWN hash (I6 single-use binding). */
  canonical_sha256: string;
  /** Required iff verb == edit_confirm. */
  amend_content?: string;
  /** Required iff verb == redirect. */
  redirect_note?: string;
  /** reject_as_false only, when the proposal carries no claim_refs (S3). */
  negation_text?: string;
}

/** Events the Review rail subscribes to (the rail never touches ProseMirror directly). */
export interface AdapterEvents {
  /** Set changed, rebuild decorations (gate condition 10). */
  "proposals:changed": { open: string[] };
  "anchor:reanchored": { proposal_id: string; from: number; to: number };
  /** Expires toward re-review, never acceptance. */
  "anchor:lost": { proposal_id: string };
  "decision:staged": { item: DecisionItem };
  "decision:cleared": { proposal_id: string };
}

export interface WbTrackedChangesAdapter {
  /** Bind to a mounted editor. Import and ingest run with engine tracking OFF (SP-1). */
  attach(editor: Editor): void;
  detach(): void;

  /**
   * Project a ledger proposal into the LOCAL, ephemeral suggestion layer. Resolves
   * quoteAnchor to (from,to), builds a replace tr, runs transformToSuggestionTransaction
   * with generateId = () => proposal_id, and dispatches under the apply-origin tag. Never
   * pushed to the server Y.Doc (surface section 1.4).
   */
  ingestProposal(p: ProposalInput): { anchored: boolean };

  /**
   * Re-anchor-by-quote fallback: on drift, relocate by quote plus context. If the anchor
   * no longer locates uniquely, emit anchor:lost and expire the proposal.
   */
  reanchor(proposal_id: string): { from: number; to: number } | null;

  /**
   * Stage or clear a per-item decision. Accept maps to applySuggestion, Reject maps to
   * revertSuggestion, Redirect and Defer leave marks in place. Staging never commits.
   */
  stageDecision(item: DecisionItem): void;
  clearDecision(proposal_id: string): void;

  /** Collect the staged sitting for R5. The route mints the gestures, not the client. */
  collectSitting(): DecisionItem[];

  /** Walk open suggestion groups by id. Display re-derives from the ledger. */
  listOpen(): string[];

  /** Apply an engine-accepted content change as an apply-origin foreign update (SP-2 6). */
  applyServerUpdate(update: Uint8Array): void;

  on<K extends keyof AdapterEvents>(ev: K, cb: (p: AdapterEvents[K]) => void): () => void;
}

/**
 * The R5 sitting wire shapes (surface section 1.5), the ONLY decision path. The client
 * collects DecisionItems, optionally applies accepted edits to its Y.Doc and posts the
 * rendered Markdown, and the route mints the gestures.
 */

export interface MaterializePayload {
  /** The block-spliced Markdown the client rendered after applying accepted edits. */
  readonly rendered_markdown: string;
  /** Lowercase hex SHA-256 of rendered_markdown, verified server-side. */
  readonly post_apply_content_sha256: string;
}

export interface SittingRequest {
  /** Doc hash the whole sitting was composed against (advisory concurrency). */
  readonly base_doc_sha256: string;
  readonly items: readonly DecisionItem[];
  /** Present when the sitting contains any confirm / edit_confirm. */
  readonly materialize: MaterializePayload | null;
}

export type SittingResultKind =
  | "applied"
  | "closed"
  | "kept_open_redirected"
  | "kept_open_deferred"
  | "kept_open_endorsed"
  | "rejected_stale_view"
  | "error";

export interface SittingItemResult {
  readonly proposal_id: string;
  readonly verb: SittingVerb;
  readonly result: SittingResultKind;
  /** (S6) proposal.base_content_sha256 == documents latest content hash. */
  readonly base_ok: boolean;
  /** Set for every result that minted a gesture (all but rejected_stale_view / error). */
  readonly gesture_id: string | null;
  /** reject_as_false, the minted confirmed negation (result closed). */
  readonly negation_claim_id: string | null;
  /** reject_as_preference, the recorded preference (result closed). */
  readonly preference_claim_id: string | null;
  /** kept_open_endorsed, drafted-fix linkage, null until the agent redrafts. */
  readonly new_proposal_id: string | null;
  /** applied items that reached the file this sitting. */
  readonly materialized: boolean;
  /** Set for rejected_stale_view and error. */
  readonly error: string | null;
}

export interface SittingResponse {
  readonly ok: true;
  readonly partial: boolean;
  readonly results: readonly SittingItemResult[];
  readonly materialize: { readonly file_path: string; readonly new_file_sha256: string } | null;
}
