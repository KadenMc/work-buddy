import type { CoworkDocClient } from "../../bridge/HttpCoworkDocClient";
import type { R2DocPayload } from "../../bridge/types";
import { mapProvenanceView } from "./provenanceMapping";
import type {
  ProvenanceLoad,
  ProvenanceProvider,
  ProvenanceReviewerBinding,
} from "./contracts";

export interface ProvenanceSnapshotSource {
  loadPayload(): Promise<R2DocPayload>;
  refreshPayload(): Promise<R2DocPayload>;
  subscribe(listener: () => void): () => void;
}

/** Dedicated provenance seam backed by the shared document snapshot source. */
export class LiveProvenanceProvider implements ProvenanceProvider {
  readonly #source: ProvenanceSnapshotSource;
  readonly #client: CoworkDocClient;
  readonly #pendingKeys = new Map<string, string>();

  constructor(source: ProvenanceSnapshotSource, client: CoworkDocClient) {
    this.#source = source;
    this.#client = client;
  }

  async load(): Promise<ProvenanceLoad> {
    return this.#map(await this.#source.loadPayload());
  }

  async refresh(): Promise<ProvenanceLoad> {
    return this.#map(await this.#source.refreshPayload());
  }

  #map(payload: R2DocPayload): ProvenanceLoad {
    if (payload.provenance === undefined) {
      return {
        state: "unavailable",
        reason: "This server did not provide the provenance view needed to explain the document safely.",
      };
    }
    try {
      return { state: "ready", data: mapProvenanceView(payload.provenance) };
    } catch (error) {
      return {
        state: "unavailable",
        reason:
          error instanceof Error
            ? error.message
            : "The server returned a malformed provenance view.",
      };
    }
  }

  subscribe(listener: () => void): () => void {
    return this.#source.subscribe(listener);
  }

  async markReviewed(
    attestationIds: readonly string[],
    expectedStructuredHeadSha256: string,
    expectedReviewer?: ProvenanceReviewerBinding,
  ): Promise<void> {
    if (attestationIds.length === 0) return;
    const batchMutation =
      expectedReviewer === undefined
        ? undefined
        : this.#client.markProvenanceSelectionReviewed;
    const singleMutation = this.#client.markProvenanceReviewed;
    if (batchMutation === undefined && (attestationIds.length !== 1 || singleMutation === undefined)) {
      throw new Error("Provenance review is unavailable.");
    }
    const fingerprint = `${JSON.stringify(attestationIds)}\u0000${expectedStructuredHeadSha256}\u0000${JSON.stringify(expectedReviewer ?? null)}`;
    const idempotencyKey =
      this.#pendingKeys.get(fingerprint) ??
      `provenance-review-${globalThis.crypto.randomUUID()}`;
    this.#pendingKeys.set(fingerprint, idempotencyKey);
    if (batchMutation !== undefined) {
      await batchMutation.call(
        this.#client,
        attestationIds,
        expectedStructuredHeadSha256,
        idempotencyKey,
        expectedReviewer,
      );
    } else {
      await singleMutation!.call(
        this.#client,
        attestationIds[0]!,
        expectedStructuredHeadSha256,
        idempotencyKey,
        expectedReviewer,
      );
    }
    const fresh = this.#map(await this.#source.refreshPayload());
    if (
      fresh.state === "ready" &&
      attestationIds.every((attestationId) =>
        fresh.data.history.some(
          (record) =>
            record.supersedesId === attestationId &&
            record.humanReview.status === "reviewed" &&
            (expectedReviewer === undefined ||
              record.humanReview.reviewers.some(
                (reviewer) =>
                  reviewer.ref === expectedReviewer.ref &&
                  reviewer.identityStatus === expectedReviewer.identityStatus,
              )),
        ),
      )
    ) {
      this.#pendingKeys.delete(fingerprint);
    }
  }
}
