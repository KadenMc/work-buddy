/**
 * The live ReviewRailProvider. It replaces InMemoryReviewProvider in the surface: load()
 * pulls R2 once, and that single pull feeds BOTH the rail cards (the
 * ReviewRailData it returns) AND the editor marks (the ProposalInput list it emits to the
 * ingestor), so the card set and the mark set are two projections of one array and can never
 * disagree. submitSitting drives the sitting through the adapter and R5 (sittingSubmit.ts).
 *
 * The rail talks to this only through the frozen ReviewRailProvider seam (load / subscribe /
 * submitSitting). The extra onProposals / onData subscriptions are the bridge's ingestion and
 * health channels, consumed by the surface, not by the rail. A late subscriber immediately
 * receives the last pull, so the editor ingests even when it mounts after the first load.
 *
 * The SSE nudge (section 1.11) is React-context-bound, so it stays out of this class: the
 * surface listens for the doc-scoped truth.doc_* events and calls invalidate(), which fans
 * out to the rail's reload listeners exactly as a provider-internal nudge would.
 */

import type { ReviewRailData, SittingResult } from "../rail/contracts";
import type {
  ReviewInvalidationListener,
  ReviewRailProvider,
  ReviewUnsubscribe,
  SittingSubmission,
} from "../rail/provider";
import type { CoworkSittingTransport } from "../suggestions/sitting";
import type { ProposalInput } from "../suggestions/types";
import type { RoutingDeliveryInput } from "../chat";
import type { CoworkDocClient } from "./HttpCoworkDocClient";
import { mapR2ToReview } from "./reviewMapping";
import { submitCoworkSitting, type DecisionApplier } from "./sittingSubmit";

/** Called with the ingestion inputs each time a pull resolves. */
export type ProposalsListener = (proposals: readonly ProposalInput[]) => void;
/** Called with the rail data each time a pull resolves (health-strip channel). */
export type ReviewDataListener = (data: ReviewRailData) => void;

export interface LiveReviewRailProviderOptions {
  readonly docClient: CoworkDocClient;
  readonly documentId: string;
  readonly storeId: string;
  /** The sitting transport (HttpCoworkSittingTransport live, in-memory in tests). */
  readonly sittingTransport: CoworkSittingTransport;
  /** The editor adapter, lazily resolved because the editor mounts after the rail. */
  readonly getAdapter: () => DecisionApplier | null;
  /** Render the post-apply document to Markdown for the materialize block. */
  readonly renderMaterialized: () => Promise<string>;
  /** Notified per routed item after a submit, so the Chat tab annotates the routing note. */
  readonly onRoutingDelivery?: (delivery: RoutingDeliveryInput) => void;
}

export class LiveReviewRailProvider implements ReviewRailProvider {
  readonly #options: LiveReviewRailProviderOptions;
  readonly #invalidationListeners = new Set<ReviewInvalidationListener>();
  readonly #proposalsListeners = new Set<ProposalsListener>();
  readonly #dataListeners = new Set<ReviewDataListener>();
  #lastProposals: readonly ProposalInput[] | null = null;
  #lastData: ReviewRailData | null = null;

  constructor(options: LiveReviewRailProviderOptions) {
    this.#options = options;
  }

  async load(): Promise<ReviewRailData> {
    const payload = await this.#options.docClient.fetchDoc();
    const mapped = mapR2ToReview(payload);
    this.#lastProposals = mapped.proposalInputs;
    this.#lastData = mapped.railData;
    for (const listener of this.#proposalsListeners) listener(mapped.proposalInputs);
    for (const listener of this.#dataListeners) listener(mapped.railData);
    return mapped.railData;
  }

  subscribe(onInvalidate: ReviewInvalidationListener): ReviewUnsubscribe {
    this.#invalidationListeners.add(onInvalidate);
    return () => {
      this.#invalidationListeners.delete(onInvalidate);
    };
  }

  /** Fan a doc-scoped SSE nudge out to the rail's reload listeners. */
  invalidate(): void {
    for (const listener of this.#invalidationListeners) listener();
  }

  async submitSitting(submission: SittingSubmission): Promise<SittingResult> {
    const adapter = this.#options.getAdapter();
    if (adapter === null) {
      throw new Error("the editor adapter is not ready, so the sitting cannot apply");
    }
    return submitCoworkSitting({
      documentId: this.#options.documentId,
      storeId: this.#options.storeId,
      submission,
      adapter,
      transport: this.#options.sittingTransport,
      renderMaterialized: this.#options.renderMaterialized,
      ...(this.#options.onRoutingDelivery === undefined
        ? {}
        : { onRoutingDelivery: this.#options.onRoutingDelivery }),
    });
  }

  /** The ingestion channel. A late subscriber immediately gets the last pull. */
  onProposals(listener: ProposalsListener): ReviewUnsubscribe {
    this.#proposalsListeners.add(listener);
    if (this.#lastProposals !== null) listener(this.#lastProposals);
    return () => {
      this.#proposalsListeners.delete(listener);
    };
  }

  /** The health-strip channel. A late subscriber immediately gets the last pull. */
  onData(listener: ReviewDataListener): ReviewUnsubscribe {
    this.#dataListeners.add(listener);
    if (this.#lastData !== null) listener(this.#lastData);
    return () => {
      this.#dataListeners.delete(listener);
    };
  }
}
