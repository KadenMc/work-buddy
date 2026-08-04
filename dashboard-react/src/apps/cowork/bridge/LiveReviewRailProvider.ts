/**
 * The live ReviewRailProvider. It replaces InMemoryReviewProvider in the surface: load()
 * pulls R2 once, and that authoritative snapshot feeds BOTH the rail cards and the
 * editor's view-only decoration projection, so the two surfaces cannot disagree.
 * submitSitting validates admitted decisions, prepares any content change on an isolated
 * canonical clone, and commits through R5.
 *
 * The rail talks to this only through the frozen ReviewRailProvider seam (load / subscribe /
 * submitSitting). The extra onProposals / onData subscriptions are the bridge's decoration
 * and health channels, consumed by the surface, not by the rail. A late subscriber
 * immediately receives the last pull, so the editor projects it even when mounting later.
 *
 * The SSE nudge (section 1.11) is React-context-bound, so it stays out of this class: the
 * surface listens for the doc-scoped truth.doc_* events and calls invalidate(), which fans
 * out to the rail's reload listeners exactly as a provider-internal nudge would.
 */

import type {
  ReviewRailData,
  SittingResult,
  VerificationRecheckIntent,
  VerifyCheckInput,
  VerifyCriterionDraftInput,
  VerifyRunInspection,
} from "../rail/contracts";
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
import { submitCoworkSitting } from "./sittingSubmit";
import type { CoworkSittingWorkspace } from "./sittingWorkspace";

/** Called with authoritative proposal inputs each time a pull resolves. */
export type ProposalsListener = (proposals: readonly ProposalInput[]) => void;
/** Called with the rail data each time a pull resolves (health-strip channel). */
export type ReviewDataListener = (data: ReviewRailData) => void;

/** One durable recheck intent projected from the committed sitting ledger. */
export type VerifyRecheckRequest = VerificationRecheckIntent;

export interface LiveReviewRailProviderOptions {
  readonly docClient: CoworkDocClient;
  readonly documentId: string;
  readonly storeId: string;
  /** The sitting transport (HttpCoworkSittingTransport live, in-memory in tests). */
  readonly sittingTransport: CoworkSittingTransport;
  /** Editor-owned clone/sync seam, lazily resolved because the editor mounts after the rail. */
  readonly getSittingWorkspace: () => CoworkSittingWorkspace | null;
  /** Notified per routed item after a submit, so the Chat tab annotates the routing note. */
  readonly onRoutingDelivery?: (delivery: RoutingDeliveryInput) => void;
  /** Applied Verify corrections whose committed document version is ready to recheck. */
  readonly onSittingCommitted?: (
    requests: readonly VerifyRecheckRequest[],
  ) => void;
}

export class LiveReviewRailProvider implements ReviewRailProvider {
  readonly #options: LiveReviewRailProviderOptions;
  readonly #invalidationListeners = new Set<ReviewInvalidationListener>();
  readonly #proposalsListeners = new Set<ProposalsListener>();
  readonly #dataListeners = new Set<ReviewDataListener>();
  #lastProposals: readonly ProposalInput[] | null = null;
  #lastData: ReviewRailData | null = null;
  #pendingKey: { readonly fingerprint: string; readonly key: string } | null = null;
  #loadSequence = 0;
  #latestLoad: Promise<ReviewRailData> | null = null;

  constructor(options: LiveReviewRailProviderOptions) {
    this.#options = options;
  }

  load(): Promise<ReviewRailData> {
    const sequence = ++this.#loadSequence;
    const pending = this.#performLoad(sequence);
    this.#latestLoad = pending;
    return pending;
  }

  async #performLoad(sequence: number): Promise<ReviewRailData> {
    const payload = await this.#options.docClient.fetchDoc();
    const mapped = mapR2ToReview(payload);

    // Multiple SSE nudges can overlap. A slower, older pull must never publish
    // either before or after a newer pull. Join the newest in-flight request,
    // so listeners and every caller observe one authoritative snapshot.
    if (sequence !== this.#loadSequence) {
      if (this.#latestLoad !== null) return this.#latestLoad;
      if (this.#lastData !== null) return this.#lastData;
    }

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

  /**
   * Mutation completion means the subscriber has received a fresh R2
   * projection, not merely that the write request returned. This keeps the
   * check menu and Run gate from briefly falling back to stale state.
   */
  async #invalidateAndWaitForReload(): Promise<void> {
    const sequenceBefore = this.#loadSequence;
    this.invalidate();
    if (
      this.#loadSequence > sequenceBefore &&
      this.#latestLoad !== null
    ) {
      await this.#latestLoad;
    }
  }

  async submitSitting(submission: SittingSubmission): Promise<SittingResult> {
    if (submission.claimDecisions.length > 0) {
      throw new Error(
        "Live claim review is not available yet. No sitting decisions were submitted.",
      );
    }
    const workspace = this.#options.getSittingWorkspace();
    if (workspace === null) {
      throw new Error("the editor is not ready, so the sitting cannot be prepared");
    }
    const result = await submitCoworkSitting({
      documentId: this.#options.documentId,
      storeId: this.#options.storeId,
      submission,
      workspace,
      transport: this.#options.sittingTransport,
      idempotencyKeyFor: (fingerprint) => this.#idempotencyKey(fingerprint),
      onIntentAbandoned: (fingerprint) => {
        if (this.#pendingKey?.fingerprint === fingerprint) {
          this.#pendingKey = null;
        }
      },
      onCommitted: () => {
        this.#pendingKey = null;
      },
      ...(this.#options.onRoutingDelivery === undefined
        ? {}
        : { onRoutingDelivery: this.#options.onRoutingDelivery }),
    });
    if (result.results.some((item) => item.result === "applied")) {
      // A fresh R2 pull is the only source of recheck work. It derives the
      // intent from the committed sitting receipt and therefore survives a
      // browser or sidecar restart; pre-commit client inference is forbidden.
      const refreshed = await this.load();
      const requests = refreshed.verificationRecheckIntents.filter(
        (intent) => intent.status !== "fulfilled",
      );
      if (requests.length > 0) {
        this.#options.onSittingCommitted?.(requests);
      }
    }
    return result;
  }

  async setVerifyCriterionEnabled(
    criterionKey: string,
    enabled: boolean,
    expectedActivationId: string | null,
  ): Promise<void> {
    const update = this.#options.docClient.setVerifyCriterionEnabled;
    if (update === undefined) {
      throw new Error("Verify checks are unavailable from this document provider.");
    }
    await update.call(
      this.#options.docClient,
      criterionKey,
      enabled,
      expectedActivationId,
    );
    await this.#invalidateAndWaitForReload();
  }

  async actOnCothink(
    itemId: string,
    action: "park" | "dismiss",
    canonicalSha256: string,
  ): Promise<void> {
    const update = this.#options.docClient.actOnCothink;
    if (update === undefined) {
      throw new Error("Co-think history actions are unavailable.");
    }
    await update.call(
      this.#options.docClient,
      itemId,
      action,
      canonicalSha256,
    );
    this.invalidate();
  }

  async discussCothink(
    itemId: string,
    canonicalSha256: string,
  ): Promise<{ readonly conversationId: string; readonly messageId: string }> {
    const discuss = this.#options.docClient.discussCothink;
    if (discuss === undefined) {
      throw new Error("Co-think discussion is unavailable.");
    }
    const receipt = await discuss.call(
      this.#options.docClient,
      itemId,
      canonicalSha256,
    );
    this.invalidate();
    return receipt;
  }

  async inspectVerifyRun(runId: string): Promise<VerifyRunInspection> {
    const inspect = this.#options.docClient.inspectVerifyRun;
    if (inspect === undefined) {
      throw new Error("Verify run inspection is unavailable.");
    }
    return inspect.call(this.#options.docClient, runId);
  }

  async createVerifyCriterionDraft(
    draft: VerifyCriterionDraftInput,
  ): Promise<void> {
    const create = this.#options.docClient.createVerifyCriterionDraft;
    if (create === undefined) {
      throw new Error("User-authored Verify criteria are unavailable.");
    }
    await create.call(this.#options.docClient, draft);
    this.invalidate();
  }

  async createVerifyCheck(check: VerifyCheckInput): Promise<void> {
    const create = this.#options.docClient.createVerifyCheck;
    if (create === undefined) {
      throw new Error("User-authored verification checks are unavailable.");
    }
    await create.call(this.#options.docClient, check);
    await this.#invalidateAndWaitForReload();
  }

  #idempotencyKey(fingerprint: string): string {
    if (this.#pendingKey?.fingerprint === fingerprint) return this.#pendingKey.key;
    const key =
      globalThis.crypto?.randomUUID?.() ??
      `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
    this.#pendingKey = { fingerprint, key };
    return key;
  }

  /** The authoritative proposal-catalog channel. A late subscriber gets the last pull. */
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
