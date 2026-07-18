/**
 * The rail drift-health strip. A compact summary at the top of the Review tab:
 * the document's drift state and its open-proposal and open-flag counts. Read
 * only chrome, SSE-updated through the provider reload. Drift is carried by a
 * text label as well as a data attribute, so its meaning survives forced-colors
 * (SP-6 G3, section 5.4).
 */

import type { RailDriftHealth } from "./contracts";

export interface RailDriftStripProps {
  readonly title: string;
  readonly drift: RailDriftHealth;
}

const DRIFT_LABEL: Record<RailDriftHealth["state"], string> = {
  clean: "In sync, no drift",
  drifted: "Drifted from file",
  missing: "File missing",
};

export function RailDriftStrip({ title, drift }: RailDriftStripProps) {
  const proposalOnly = drift.openProposalCount - drift.openFlagCount;
  return (
    <div className="wb-cowork-rail__drift" aria-label="Review health">
      <span className="wb-cowork-rail__drift-title" title={title}>
        {title}
      </span>
      <span className="wb-cowork-rail__drift-state" data-drift={drift.state}>
        {DRIFT_LABEL[drift.state]}
      </span>
      <span className="wb-cowork-rail__drift-count">
        {proposalOnly} suggestion{proposalOnly === 1 ? "" : "s"},{" "}
        {drift.openFlagCount} flag{drift.openFlagCount === 1 ? "" : "s"}
      </span>
    </div>
  );
}
