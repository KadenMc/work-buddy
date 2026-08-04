/**
 * User-actionable recovery for a Review submission that is valid item-by-item but
 * cannot be applied as one document change. The provider rejects the original
 * submission without mutating the document; Review may then offer the explicitly
 * identified non-blocked subset as a separate user-confirmed submission.
 */

export type DecisionApplyBlockReason =
  | "conflicts_with_selected_edit"
  | "passage_unavailable"
  | "proposal_unavailable"
  | "proposal_changed"
  | "not_editable"
  | "not_currently_applicable";

export interface DecisionApplyBlocker {
  readonly proposalId: string;
  readonly reason: DecisionApplyBlockReason;
  readonly relatedProposalIds: readonly string[];
  readonly message: string;
}

export interface DecisionApplyRecovery {
  readonly availableProposalIds: readonly string[];
  readonly blockers: readonly DecisionApplyBlocker[];
}

/**
 * A failed preparation with a deterministic, non-mutating recovery. This is not
 * an HTTP error: no decisions have been committed when it is raised.
 */
export class RecoverableDecisionApplyError extends Error {
  readonly recovery: DecisionApplyRecovery;

  constructor(message: string, recovery: DecisionApplyRecovery) {
    super(message);
    this.name = "RecoverableDecisionApplyError";
    this.recovery = recovery;
  }
}
