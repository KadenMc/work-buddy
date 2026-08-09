import type { TruthClaimSummary, TruthRailProvider } from "./contracts";
import { useTruthData } from "./useTruthData";
import "./styles.css";

const needsAttention = (claim: TruthClaimSummary): boolean =>
  claim.needsReview ||
  claim.baseStatus === "proposed" ||
  claim.baseStatus === "challenged";

const attentionLabel = (claim: TruthClaimSummary): string =>
  claim.needsReview
    ? "Needs review"
    : claim.baseStatus === "proposed"
      ? "Proposed"
      : "Challenged";

export interface TruthAttentionFeedProps {
  readonly provider: TruthRailProvider;
  onOpenClaim(claimId: string): void;
}

/** A passive Review companion: it routes attention, but never decides claims. */
export function TruthAttentionFeed({
  provider,
  onOpenClaim,
}: TruthAttentionFeedProps) {
  const { data, status } = useTruthData(provider, {
    scope: "document",
    filter: "all",
  });
  // This is a supplementary cross-list inside Review. Loading or an isolated
  // Truth failure must not insert transient rows that shift Review's geometry;
  // the first-class Truth tab owns those states and retry controls.
  if (status !== "ready") return null;
  const claims = (data?.claims ?? []).filter(needsAttention);
  if (claims.length === 0) return null;
  return (
    <section className="wb-cowork-truth-attention" aria-label="Truth needing attention">
      <div className="wb-cowork-truth-attention__head">
        <h3>Truth needs attention</h3>
        <span>{claims.length}</span>
      </div>
      <ul>
        {claims.map((claim) => (
          <li key={claim.claimId}>
            <button type="button" onClick={() => onOpenClaim(claim.claimId)}>
              <span>{claim.proposition}</span>
              <small>{attentionLabel(claim)}</small>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
