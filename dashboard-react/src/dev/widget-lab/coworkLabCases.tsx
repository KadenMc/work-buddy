/**
 * Co-work states for the Widget Lab. The Co-work surface is a single-surface view,
 * not a registered grid widget, so it cannot ride the WidgetHost cases the rest of
 * the lab builds. This section renders the review-rail family directly at the
 * states a reviewer needs to eyeball: each card type, each verb group, the
 * stale-base disabled state, and the narrow grouped fallback. It draws on the same
 * shipped demo scene the rail tests use, so the lab and the tests never drift.
 */

import { useState, type ReactNode } from "react";

import {
  ClaimCard,
  MarkBar,
  ProposalCard,
  StreamView,
  demoReviewData,
  orderedItems,
  type MarkBarTarget,
  type ReviewClaim,
  type ReviewProposal,
  type StagedClaimDecision,
  type StagedDecision,
} from "../../apps/cowork/rail";
import "../../apps/cowork/rail/styles.css";

const DATA = demoReviewData();

function proposalOfKind(
  kind: "insertion" | "deletion" | "flag",
): ReviewProposal {
  const match = DATA.proposals.find((proposal) =>
    kind === "flag"
      ? proposal.kind === "flag"
      : proposal.kind === "edit" && proposal.changeType === kind,
  );
  if (match === undefined) {
    throw new Error(`The demo scene must carry a ${kind} proposal.`);
  }
  return match;
}

const insertion = proposalOfKind("insertion");
const deletion = proposalOfKind("deletion");
const flag = proposalOfKind("flag");
const claim: ReviewClaim = (() => {
  const first = DATA.claims[0];
  if (first === undefined) throw new Error("The demo scene must carry a claim.");
  return first;
})();
const staleBase: ReviewProposal = { ...insertion, proposalId: "stale-1", baseOk: false };

const noop = () => undefined;

/** A mark bar wired to local staging so the verb affordances are live in the lab. */
function LabMarkBar({
  target,
  testId,
}: {
  readonly target: MarkBarTarget;
  readonly testId: string;
}) {
  const [proposalDecision, setProposalDecision] = useState<StagedDecision>();
  const [claimDecision, setClaimDecision] = useState<StagedClaimDecision>();
  return (
    <div data-testid={testId} className="wb-cowork-lab__markbar-host">
      <MarkBar
        target={target}
        stagedProposal={proposalDecision}
        stagedClaim={claimDecision}
        showHotkeys
        onStageProposal={setProposalDecision}
        onStageClaim={setClaimDecision}
        onClearProposal={() => setProposalDecision(undefined)}
        onClearClaim={() => setClaimDecision(undefined)}
      />
    </div>
  );
}

function LabPanel({
  heading,
  testId,
  children,
}: {
  readonly heading: string;
  readonly testId: string;
  readonly children: ReactNode;
}) {
  return (
    <section className="wb-cowork-lab__panel" data-testid={testId}>
      <h3 className="wb-cowork-lab__panel-head">{heading}</h3>
      {children}
    </section>
  );
}

/** The Co-work Widget Lab section, mounted alongside the reusable-widget states. */
export function CoworkLabSection() {
  const items = orderedItems(DATA);
  return (
    <section aria-labelledby="widget-lab-cowork" className="wb-cowork-lab">
      <h2 id="widget-lab-cowork">Co-work review states</h2>
      <p>
        The single-surface review rail family at the states that matter for
        conformance: every card type, every verb group, the stale-base disabled
        state, and the narrow grouped fallback.
      </p>

      <div className="wb-cowork-lab__grid">
        <LabPanel heading="Insertion card" testId="cowork-lab-card-insertion">
          <ul className="wb-cowork-rail__card-list">
            <ProposalCard proposal={insertion} selected onSelect={noop} onScrollToAnchor={noop} />
          </ul>
        </LabPanel>

        <LabPanel heading="Deletion card" testId="cowork-lab-card-deletion">
          <ul className="wb-cowork-rail__card-list">
            <ProposalCard proposal={deletion} selected={false} onSelect={noop} />
          </ul>
        </LabPanel>

        <LabPanel heading="Flag card" testId="cowork-lab-card-flag">
          <ul className="wb-cowork-rail__card-list">
            <ProposalCard proposal={flag} selected={false} onSelect={noop} />
          </ul>
        </LabPanel>

        <LabPanel heading="Claim card" testId="cowork-lab-card-claim">
          <ul className="wb-cowork-rail__card-list">
            <ClaimCard
              claim={claim}
              selected={false}
              onSelect={noop}
              inspectSpanId="sp-cl1"
              onInspect={noop}
            />
          </ul>
        </LabPanel>

        <LabPanel heading="Edit verbs" testId="cowork-lab-verbs-edit">
          <LabMarkBar
            target={{ kind: "proposal", proposal: insertion }}
            testId="cowork-lab-markbar-edit"
          />
        </LabPanel>

        <LabPanel heading="Flag verbs" testId="cowork-lab-verbs-flag">
          <LabMarkBar
            target={{ kind: "proposal", proposal: flag }}
            testId="cowork-lab-markbar-flag"
          />
        </LabPanel>

        <LabPanel heading="Claim verbs" testId="cowork-lab-verbs-claim">
          <LabMarkBar
            target={{ kind: "claim", claim }}
            testId="cowork-lab-markbar-claim"
          />
        </LabPanel>

        <LabPanel
          heading="Stale-base disabled"
          testId="cowork-lab-stale"
        >
          <ul className="wb-cowork-rail__card-list">
            <ProposalCard proposal={staleBase} selected onSelect={noop} />
          </ul>
          <LabMarkBar
            target={{ kind: "proposal", proposal: staleBase }}
            testId="cowork-lab-markbar-stale"
          />
        </LabPanel>

        <LabPanel
          heading="Narrow grouped fallback"
          testId="cowork-lab-grouped"
        >
          <StreamView
            items={items}
            selectedId={null}
            decisions={{}}
            claimDecisions={{}}
            inspectSpanByClaim={new Map()}
            grouped
            onSelect={noop}
            onInspect={noop}
          />
        </LabPanel>
      </div>
    </section>
  );
}

export default CoworkLabSection;
