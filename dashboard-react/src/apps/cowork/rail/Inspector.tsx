/**
 * The click-a-sentence inspector (PRD job 4). Read-only in v1. Given a document
 * span it shows what is underneath the passage: the expressions (claims, with a
 * status chip), the provenance (who wrote it and who approved it), and any open
 * proposals anchored there. It never mutates, it only reveals the ledger state
 * already carried by the review layer.
 */

import type {
  ExpressionClaimStatus,
  ReviewRailData,
  TrustState,
} from "./contracts";

export interface InspectorProps {
  readonly spanId: string;
  readonly data: ReviewRailData;
  onClose(): void;
}

const CLAIM_STATUS_LABEL: Record<ExpressionClaimStatus, string> = {
  confirmed: "Confirmed",
  needs_review: "Needs review",
  proposed: "Proposed",
  rejected: "Rejected",
};

const TRUST_LABEL: Record<TrustState, string> = {
  human: "Human-written",
  ai_confirmed: "AI-written, human-confirmed",
  ai_proposed: "AI-proposed, unconfirmed",
};

export function Inspector({ spanId, data, onClose }: InspectorProps) {
  const expressions = data.expressions.filter(
    (expression) => expression.spanId === spanId,
  );
  const provenance = data.provenanceSpans.find((span) => span.spanId === spanId);
  const quote = expressions[0]?.quote ?? provenance?.quote ?? "";
  const openItems = data.proposals.filter(
    (proposal) =>
      quote.length > 0 && quote.includes(proposal.quoteAnchor.exact.trim()),
  );

  return (
    <section
      className="wb-cowork-rail__inspector"
      aria-label="Passage inspector"
    >
      <div className="wb-cowork-rail__inspector-head">
        <h3 className="wb-cowork-rail__inspector-title">Under this sentence</h3>
        <button
          type="button"
          className="wb-cowork-rail__inspector-close"
          onClick={onClose}
        >
          Close
        </button>
      </div>

      {quote.length > 0 ? (
        <blockquote className="wb-cowork-rail__inspector-quote">
          {quote}
        </blockquote>
      ) : null}

      <div className="wb-cowork-rail__inspector-section">
        <h4 className="wb-cowork-rail__inspector-label">Expresses</h4>
        {expressions.length === 0 ? (
          <p className="wb-cowork-rail__inspector-empty">
            No claims are linked to this passage yet.
          </p>
        ) : (
          <ul className="wb-cowork-rail__inspector-list">
            {expressions.map((expression) => (
              <li
                key={expression.expressionId}
                className="wb-cowork-rail__inspector-item"
              >
                <span className="wb-cowork-rail__inspector-claim">
                  {expression.claimRef}
                </span>
                {expression.claimStatus !== null ? (
                  <span
                    className="wb-cowork-rail__claim-status"
                    data-status={expression.claimStatus}
                  >
                    {CLAIM_STATUS_LABEL[expression.claimStatus]}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="wb-cowork-rail__inspector-section">
        <h4 className="wb-cowork-rail__inspector-label">Provenance</h4>
        {provenance === undefined ? (
          <p className="wb-cowork-rail__inspector-empty">
            No provenance is recorded for this passage.
          </p>
        ) : (
          <dl className="wb-cowork-rail__inspector-prov">
            <div>
              <dt>Trust</dt>
              <dd data-trust={provenance.trustState}>
                {TRUST_LABEL[provenance.trustState]}
              </dd>
            </div>
            {provenance.producer !== null ? (
              <div>
                <dt>Producer</dt>
                <dd>{provenance.producer.model}</dd>
              </div>
            ) : null}
            {provenance.approvalGestureId !== null ? (
              <div>
                <dt>Approved by</dt>
                <dd>{provenance.approvalGestureId}</dd>
              </div>
            ) : null}
          </dl>
        )}
      </div>

      <div className="wb-cowork-rail__inspector-section">
        <h4 className="wb-cowork-rail__inspector-label">Open items here</h4>
        {openItems.length === 0 ? (
          <p className="wb-cowork-rail__inspector-empty">
            Nothing is open on this passage.
          </p>
        ) : (
          <ul className="wb-cowork-rail__inspector-list">
            {openItems.map((proposal) => (
              <li
                key={proposal.proposalId}
                className="wb-cowork-rail__inspector-item"
              >
                {proposal.kind === "flag" ? "Flag" : "Edit"}: {proposal.tldr}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
