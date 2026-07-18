/**
 * The ingestion coordinator. It takes the ProposalInput list the pull mapped (the SAME
 * array the rail cards come from) and projects each open proposal into the editor through
 * WbTrackedChangesAdapter.ingestProposal, so the suggestion marks render from the one source
 * of truth the cards render from. Ingestion is idempotent per proposal id: a proposal is
 * projected at most once, so a reload (the SSE nudge) adds only newly-open proposals and
 * never double-applies an already-ingested one.
 *
 * Timing is decoupled: the pull can arrive before the editor mounts or after. setProposals
 * records the latest set, attach records the adapter once the editor is ready, and each call
 * flushes whatever it can. A proposal whose quote does not yet anchor is left un-anchored so
 * a later pull (after the doc settles) can retry it, rather than being marked done and lost.
 *
 * Decided proposals leave the open set through the sitting submit path, which applies the
 * accept or reject to the editor itself (adapter.applyDecision), so their marks are already
 * resolved before the reduced set arrives here. A proposal that expires server-side with no
 * local decision is the one case whose mark this incremental projector does not retract in
 * v1: the rail card set (reloaded from R2) is always authoritative, and a stale insertion
 * mark self-corrects on the human's next reject or a full remount.
 */

import type { ProposalInput, WbTrackedChangesAdapter } from "../suggestions/types";

export class ProposalIngestor {
  #adapter: WbTrackedChangesAdapter | null = null;
  #proposals: readonly ProposalInput[] = [];
  /** Proposal ids successfully projected into the editor, so we never re-ingest them. */
  readonly #anchored = new Set<string>();

  /** Bind the adapter once the editor is mounted and the adapter attached to it. */
  attach(adapter: WbTrackedChangesAdapter): void {
    this.#adapter = adapter;
    this.#flush();
  }

  /** Release the adapter and forget what was projected, so a remount re-ingests cleanly. */
  detach(): void {
    this.#adapter = null;
    this.#anchored.clear();
  }

  /** Record the latest open-proposal set from a pull and project what can be anchored. */
  setProposals(proposals: readonly ProposalInput[]): void {
    this.#proposals = proposals;
    this.#flush();
  }

  /** The proposal ids currently projected as marks, for the cards-equal-marks assertion. */
  anchoredIds(): readonly string[] {
    return [...this.#anchored];
  }

  #flush(): void {
    const adapter = this.#adapter;
    if (adapter === null) return;
    for (const proposal of this.#proposals) {
      if (this.#anchored.has(proposal.proposal_id)) continue;
      const { anchored } = adapter.ingestProposal(proposal);
      // A flag anchors a span without minting a mark, and it is still "projected" for the
      // purpose of not re-ingesting it. Only an un-anchored proposal is left to retry.
      if (anchored) this.#anchored.add(proposal.proposal_id);
    }
  }
}
