import * as Y from "yjs";

import { applyForeignUpdate, isLocalHumanOrigin } from "../editor/applyOrigin";
import { sha256Hex } from "./hashing";
import type { CoworkYdocTransport } from "./transport";

/**
 * Binds a local Y.Doc to a Co-work document transport (R3 pull / R4 push, C3 opaque
 * blobs). It realizes the apply-origin persistence discipline (section 1.4): pulled
 * snapshots and batches are applied under the apply-origin tag so they never enter the
 * local undo stack, and ONLY local human-origin updates are pushed through R4. Proposal
 * ingestion and accept mutations are apply-origin too, so they are never persisted here.
 *
 * The load-order contract (SP-2) mandates hydrating the Y.Doc from persistence BEFORE
 * mounting the editor, so callers must `await hydrate()` and only then mount and
 * `start()`.
 */
export class CoworkYdocPersistence {
  readonly #doc: Y.Doc;
  readonly #transport: CoworkYdocTransport;
  #offset = "0";
  #docSha256 = "";
  #started = false;
  #chain: Promise<void> = Promise.resolve();

  constructor(doc: Y.Doc, transport: CoworkYdocTransport) {
    this.#doc = doc;
    this.#transport = transport;
  }

  /** The opaque cursor the client last consumed (for an offset-sliced pull). */
  get offset(): string {
    return this.#offset;
  }

  /** The latest server content hash the client has observed (its push base). */
  get docSha256(): string {
    return this.#docSha256;
  }

  /**
   * Resolve the persistence pull first and apply the snapshot then the batches to the
   * Y.Doc as apply-origin foreign updates, BEFORE the editor is mounted (SP-2 point 1).
   * Returns whether the document was brand-new (nothing to apply), which is the reliable
   * seed signal: the editor's own empty-doc sync can make the fragment non-empty before a
   * post-mount emptiness check runs, so seeding keys off what persistence pulled instead.
   */
  async hydrate(): Promise<{ readonly wasEmpty: boolean }> {
    const pull = await this.#transport.pull({});
    if (pull.snapshot !== null) {
      applyForeignUpdate(this.#doc, pull.snapshot);
    }
    for (const batch of pull.batches) {
      applyForeignUpdate(this.#doc, batch);
    }
    this.#offset = pull.nextOffset;
    this.#docSha256 = pull.docSha256;
    return { wasEmpty: pull.snapshot === null && pull.batches.length === 0 };
  }

  /** Begin pushing local human edits. Idempotent. */
  start(): void {
    if (this.#started) return;
    this.#started = true;
    this.#doc.on("update", this.#onUpdate);
  }

  /** Stop pushing local human edits. Idempotent. */
  stop(): void {
    if (!this.#started) return;
    this.#started = false;
    this.#doc.off("update", this.#onUpdate);
  }

  /** Await every queued push, so tests observe a settled server state. */
  async flush(): Promise<void> {
    await this.#chain;
  }

  /** Offset-sliced pull: apply only the batches appended after the client's cursor. */
  async pullSince(): Promise<void> {
    const pull = await this.#transport.pull({ sinceOffset: this.#offset });
    if (pull.snapshot !== null) {
      applyForeignUpdate(this.#doc, pull.snapshot);
    }
    for (const batch of pull.batches) {
      applyForeignUpdate(this.#doc, batch);
    }
    this.#offset = pull.nextOffset;
    this.#docSha256 = pull.docSha256;
  }

  /**
   * Client-driven compaction: encode the whole doc state as one snapshot blob and push
   * it as a compaction rider, so the server content-addresses it and truncates the
   * superseded update log (section 1.4). The client owns all Yjs computation (C3).
   */
  async compact(): Promise<void> {
    const snapshot = Y.encodeStateAsUpdate(this.#doc);
    const snapshotSha256 = await sha256Hex(snapshot);
    const result = await this.#transport.push({
      batch: snapshot,
      baseSha256: this.#docSha256,
      compaction: { snapshot, snapshotSha256 },
    });
    if (result.ok) {
      this.#offset = result.nextOffset;
      this.#docSha256 = result.docSha256;
      return;
    }
    // Another writer advanced the server, so catch up and compact once more against it.
    await this.pullSince();
    const retry = await this.#transport.push({
      batch: Y.encodeStateAsUpdate(this.#doc),
      baseSha256: this.#docSha256,
      compaction: { snapshot, snapshotSha256 },
    });
    if (retry.ok) {
      this.#offset = retry.nextOffset;
      this.#docSha256 = retry.docSha256;
    }
  }

  readonly #onUpdate = (update: Uint8Array, origin: unknown): void => {
    // R4 carries HUMAN DIRECT EDITS ONLY. Apply-origin updates (proposal ingestion,
    // accepts, pulled batches) are never pushed (section 1.4 apply-origin discipline).
    if (!isLocalHumanOrigin(origin)) return;
    const batch = new Uint8Array(update);
    this.#chain = this.#chain.then(() => this.#pushOnce(batch));
  };

  async #pushOnce(batch: Uint8Array, isRetry = false): Promise<void> {
    const result = await this.#transport.push({
      batch,
      baseSha256: this.#docSha256,
    });
    if (result.ok) {
      this.#offset = result.nextOffset;
      this.#docSha256 = result.docSha256;
      return;
    }
    if (isRetry) return;
    // Stale base: pull the missed remote batches, then re-push this batch once.
    await this.pullSince();
    await this.#pushOnce(batch, true);
  }
}
