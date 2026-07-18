import { sha256Hex } from "./hashing";
import { frameSegments } from "./framing";
import type {
  CoworkYdocPull,
  CoworkYdocPullRequest,
  CoworkYdocPushRequest,
  CoworkYdocPushResult,
  CoworkYdocTransport,
} from "./transport";

const EMPTY = new Uint8Array(0);

/**
 * An in-memory stand-in for the opaque-blob server (section 1.4), for persistence tests. It
 * models exactly the behavior a JS-less Flask handler can build: it stores a compacted
 * snapshot blob plus an append log of opaque update batches, slices the log by an
 * offset, content-addresses a compaction blob and verifies it re-hashes to the declared
 * digest, and truncates the superseded log on compaction. It NEVER interprets, merges,
 * or diffs the Yjs bytes.
 *
 * The `docSha256` fingerprint is computed over the framed stored blobs as a faithful
 * stand-in for the server content hash, so the optimistic-concurrency `stale_base` path
 * is exercisable: a push whose `baseSha256` does not match the current server state is
 * rejected without appending.
 */
export class InMemoryCoworkYdocTransport implements CoworkYdocTransport {
  #snapshot: Uint8Array | null = null;
  #snapshotSha256: string | null = null;
  #log: Uint8Array[] = [];
  /** Absolute offset of the first entry currently in `#log` (advances on compaction). */
  #baseOffset = 0;
  #docSha256 = "";

  constructor() {
    // The empty-store fingerprint, so a first push can base against a known hash.
    void this.#recomputeDocSha256();
  }

  async pull(request: CoworkYdocPullRequest): Promise<CoworkYdocPull> {
    await this.#ensureFingerprint();
    const nextOffset = String(this.#baseOffset + this.#log.length);
    if (request.sinceOffset === undefined) {
      return {
        snapshot: this.#snapshot,
        snapshotSha256: this.#snapshotSha256,
        batches: this.#log.map((batch) => new Uint8Array(batch)),
        docSha256: this.#docSha256,
        nextOffset,
      };
    }
    const since = Number(request.sinceOffset);
    if (!Number.isFinite(since) || since < this.#baseOffset) {
      // The caller is behind the snapshot boundary and cannot be served incrementally,
      // so fall back to a full pull (snapshot plus the whole current log).
      return {
        snapshot: this.#snapshot,
        snapshotSha256: this.#snapshotSha256,
        batches: this.#log.map((batch) => new Uint8Array(batch)),
        docSha256: this.#docSha256,
        nextOffset,
      };
    }
    const start = since - this.#baseOffset;
    return {
      snapshot: null,
      snapshotSha256: null,
      batches: this.#log.slice(start).map((batch) => new Uint8Array(batch)),
      docSha256: this.#docSha256,
      nextOffset,
    };
  }

  async push(request: CoworkYdocPushRequest): Promise<CoworkYdocPushResult> {
    await this.#ensureFingerprint();
    if (request.baseSha256 !== this.#docSha256) {
      return { ok: false, error: "stale_base", serverDocSha256: this.#docSha256 };
    }

    if (request.compaction !== undefined) {
      const recomputed = await sha256Hex(request.compaction.snapshot);
      if (recomputed !== request.compaction.snapshotSha256) {
        // The server content-addresses the blob and verifies the declared digest.
        throw new Error("Compaction snapshot does not re-hash to its declared digest");
      }
    }

    this.#log.push(new Uint8Array(request.batch));

    if (request.compaction !== undefined) {
      this.#snapshot = new Uint8Array(request.compaction.snapshot);
      this.#snapshotSha256 = request.compaction.snapshotSha256;
      // Truncate the now-superseded update log, since the snapshot subsumes every batch.
      this.#baseOffset += this.#log.length;
      this.#log = [];
    }

    await this.#recomputeDocSha256();
    return {
      ok: true,
      applied: true,
      docSha256: this.#docSha256,
      nextOffset: String(this.#baseOffset + this.#log.length),
    };
  }

  /** Test accessor: the current content fingerprint. */
  get docSha256(): string {
    return this.#docSha256;
  }

  /** Test accessor: how many batches remain in the append log after any compaction. */
  get pendingBatchCount(): number {
    return this.#log.length;
  }

  /** Test accessor: whether a compacted snapshot is stored. */
  get hasSnapshot(): boolean {
    return this.#snapshot !== null;
  }

  async #ensureFingerprint(): Promise<void> {
    if (this.#docSha256 === "") {
      await this.#recomputeDocSha256();
    }
  }

  async #recomputeDocSha256(): Promise<void> {
    this.#docSha256 = await sha256Hex(
      frameSegments([this.#snapshot ?? EMPTY, ...this.#log]),
    );
  }
}
