import type { CoworkDocClient } from "../../bridge/HttpCoworkDocClient";
import type { R2DocPayload } from "../../bridge/types";
import { mapProvenanceView } from "./provenanceMapping";
import type { ProvenanceLoad, ProvenanceProvider } from "./contracts";

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
    attestationId: string,
    expectedStructuredHeadSha256: string,
  ): Promise<void> {
    const mutation = this.#client.markProvenanceReviewed;
    if (mutation === undefined) throw new Error("Provenance review is unavailable.");
    const fingerprint = `${attestationId}\u0000${expectedStructuredHeadSha256}`;
    const idempotencyKey =
      this.#pendingKeys.get(fingerprint) ??
      `provenance-review-${globalThis.crypto.randomUUID()}`;
    this.#pendingKeys.set(fingerprint, idempotencyKey);
    await mutation.call(
      this.#client,
      attestationId,
      expectedStructuredHeadSha256,
      idempotencyKey,
    );
    const fresh = this.#map(await this.#source.refreshPayload());
    if (
      fresh.state === "ready" &&
      fresh.data.history.some(
        (record) =>
          record.supersedesId === attestationId &&
          record.humanReview.status === "reviewed",
      )
    ) {
      this.#pendingKeys.delete(fingerprint);
    }
  }
}
