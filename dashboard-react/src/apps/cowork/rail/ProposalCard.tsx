/**
 * One margin card for a proposal or a flag (SP-6 variant A card, rendered the
 * same way in the stream, the groups, and the queue). The card is selectable
 * (selecting it points the mark bar at it), it names its anchor with a
 * scroll-to affordance (the degrade path when true alignment is not wired), and
 * it surfaces the stale-base and staged-decision states with a non-color
 * encoding as well as color (SP-6 G3).
 */

import type { ReviewProposal, StagedDecision } from "./contracts";
import { PROPOSAL_VERB_LABEL } from "./verbs";

export interface ProposalCardProps {
  readonly proposal: ReviewProposal;
  readonly selected: boolean;
  readonly staged?: StagedDecision;
  onSelect(): void;
  /** Bring the anchor into view and flash it. Absent hides the affordance. */
  onScrollToAnchor?(): void;
  /** Ref for the aligned-stream geometry controller. */
  cardRef?: (element: HTMLElement | null) => void;
}

function kindLabel(proposal: ReviewProposal): string {
  if (proposal.kind === "flag") return "Flag";
  if (proposal.changeType === "deletion") return "Deletion";
  if (proposal.changeType === "modification") return "Modification";
  return "Insertion";
}

function kindToken(proposal: ReviewProposal): string {
  if (proposal.kind === "flag") return "flag";
  if (proposal.changeType === "deletion") return "deletion";
  if (proposal.changeType === "modification") return "modification";
  return "insertion";
}

export function ProposalCard({
  proposal,
  selected,
  staged,
  onSelect,
  onScrollToAnchor,
  cardRef,
}: ProposalCardProps) {
  const token = kindToken(proposal);
  return (
    <li
      ref={cardRef}
      className="wb-cowork-rail__card"
      data-kind={token}
      data-selected={selected ? "true" : undefined}
      data-staged={staged !== undefined ? "true" : undefined}
      data-stale={!proposal.baseOk ? "true" : undefined}
    >
      <div className="wb-cowork-rail__card-head">
        <span className="wb-cowork-rail__card-kind" data-kind={token}>
          {kindLabel(proposal)}
        </span>
        <span className="wb-cowork-rail__card-agent">
          {proposal.producer.model}
        </span>
        {onScrollToAnchor !== undefined ? (
          <button
            type="button"
            className="wb-cowork-rail__card-jump"
            onClick={onScrollToAnchor}
            aria-label={`Go to ${proposal.anchorLabel} in the document`}
          >
            {proposal.anchorLabel}
          </button>
        ) : (
          <span className="wb-cowork-rail__card-anchor">
            {proposal.anchorLabel}
          </span>
        )}
      </div>

      <button
        type="button"
        className="wb-cowork-rail__card-select"
        aria-pressed={selected}
        onClick={onSelect}
      >
        <span className="wb-cowork-rail__card-tldr">{proposal.tldr}</span>
      </button>

      {proposal.kind === "edit" && proposal.replacement !== null ? (
        <p className="wb-cowork-rail__card-quote">
          <span className="wb-cowork-rail__quote-context">
            {proposal.quoteAnchor.prefix}
          </span>
          {proposal.changeType === "deletion" ? (
            <del className="wb-cowork-rail__quote-del">
              {proposal.quoteAnchor.exact}
            </del>
          ) : (
            <ins className="wb-cowork-rail__quote-ins">
              {proposal.replacement}
            </ins>
          )}
          <span className="wb-cowork-rail__quote-context">
            {proposal.quoteAnchor.suffix}
          </span>
        </p>
      ) : null}

      <p className="wb-cowork-rail__card-rationale">{proposal.rationale}</p>

      {!proposal.baseOk ? (
        <p className="wb-cowork-rail__card-badge is-stale">
          Stale base, reject or defer only
        </p>
      ) : null}

      {proposal.fixesRef !== null ? (
        <p className="wb-cowork-rail__card-badge is-fix">Drafted fix pending</p>
      ) : null}

      {staged !== undefined ? (
        <p className="wb-cowork-rail__card-badge is-staged" role="status">
          Staged: {PROPOSAL_VERB_LABEL[staged.verb]}
        </p>
      ) : null}
    </li>
  );
}
