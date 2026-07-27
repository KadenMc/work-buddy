import * as Y from "yjs";

import { applyForeignUpdate, isLocalHumanOrigin } from "../editor/applyOrigin";
import type {
  CoworkYdocOutbox,
  CoworkYdocOutboxEntry,
} from "./CoworkYdocOutbox";
import { sha256Hex } from "./hashing";
import type { CoworkYdocTransport } from "./transport";

export type CoworkSyncStatus =
  | "hydrating"
  | "clean"
  | "saving"
  | "saved_on_device"
  | "retrying"
  | "offline"
  | "conflict"
  | "error"
  | "read_only";

export interface CoworkYdocPersistenceOptions {
  readonly outbox?: CoworkYdocOutbox;
  readonly requireSnapshot?: boolean;
  readonly readOnly?: boolean;
}

export interface CoworkCompactionReceipt {
  readonly snapshotSha256: string;
  readonly structuredHeadSha256: string;
}

/**
 * A logical generation change means the server destructively replaced the Y.Doc.
 * A live Y.Doc cannot be cleared or replaced in place: applying the replacement snapshot
 * would retain deleted state from the old generation, so the session must remount.
 */
export class CoworkYdocGenerationChangedError extends Error {
  readonly code = "cowork_ydoc_generation_changed";

  constructor() {
    super(
      "The server replaced this structured document while it was open. Co-work kept your local edits on this device and stopped before combining the two versions. Reopen the document to continue.",
    );
    this.name = "CoworkYdocGenerationChangedError";
  }
}

interface PendingBatch {
  readonly batch: Uint8Array;
  readonly outboxId?: number;
  acknowledged: boolean;
}

const NETWORK_BATCH_IDLE_MS = 250;

/**
 * Binds a local Y.Doc to a Co-work document transport (R3 pull / R4 push, C3 opaque
 * blobs). It realizes the apply-origin persistence discipline (section 1.4): pulled
 * snapshots and batches are applied under the apply-origin tag so they never enter the
 * local undo stack, and ONLY local human-origin updates are pushed through R4. Proposal
 * ingestion and accept mutations are apply-origin too, so they are never persisted here.
 *
 * The load-order contract (SP-2) is strict: `await hydrate()`, then `start()` observing,
 * then mount the editor. Starting observation before Tiptap binds ensures any structural
 * base it creates is durably ordered before human updates that depend on it. `start()` is
 * idempotent so the mounted editor may call it defensively too.
 */
export class CoworkYdocPersistence {
  readonly #doc: Y.Doc;
  readonly #transport: CoworkYdocTransport;
  readonly #outbox?: CoworkYdocOutbox;
  readonly #requireSnapshot: boolean;
  #readOnly: boolean;
  #offset = "0";
  #docSha256 = "";
  #snapshotSha256: string | null = null;
  #ydocGeneration = "";
  #hydrated = false;
  #started = false;
  #policyEpoch = 0;
  #chain: Promise<void> = Promise.resolve();
  // `#durabilityChain` is the latest append attempt and deliberately retains rejection so
  // `flush()`/`dispose()` cannot mistake an IndexedDB failure for success. `#durabilityTail`
  // is the recovered sequencing tail that lets an explicit retry (or a later edit) attempt
  // the retained, not-yet-durable batches again without permanently poisoning the queue.
  #durabilityChain: Promise<void> = Promise.resolve();
  #durabilityTail: Promise<void> = Promise.resolve();
  readonly #pendingDurabilityBatches: Uint8Array[] = [];
  #durabilityError: unknown = null;
  readonly #pendingBatches: PendingBatch[] = [];
  #lastError: unknown = null;
  #drainTimer: ReturnType<typeof setTimeout> | null = null;
  #status: CoworkSyncStatus;
  readonly #statusListeners = new Set<(status: CoworkSyncStatus) => void>();

  constructor(
    doc: Y.Doc,
    transport: CoworkYdocTransport,
    options: CoworkYdocPersistenceOptions = {},
  ) {
    this.#doc = doc;
    this.#transport = transport;
    this.#outbox = options.outbox;
    this.#requireSnapshot = options.requireSnapshot ?? false;
    this.#readOnly = options.readOnly ?? false;
    this.#status = this.#readOnly ? "read_only" : "hydrating";
  }

  /** The opaque cursor the client last consumed (for an offset-sliced pull). */
  get offset(): string {
    return this.#offset;
  }

  /** The latest server content hash the client has observed (its push base). */
  get docSha256(): string {
    return this.#docSha256;
  }

  /** Number of local update batches that have not been acknowledged remotely. */
  get pendingBatchCount(): number {
    return this.#pendingBatches.filter((entry) => !entry.acknowledged).length;
  }

  /** Latest persistence failure, cleared after a subsequent successful drain. */
  get lastError(): unknown {
    return this.#lastError;
  }

  get status(): CoworkSyncStatus {
    return this.#status;
  }

  get readOnly(): boolean {
    return this.#readOnly;
  }

  subscribeStatus(listener: (status: CoworkSyncStatus) => void): () => void {
    this.#statusListeners.add(listener);
    listener(this.#status);
    return () => this.#statusListeners.delete(listener);
  }

  /**
   * Resolve the persistence pull first and apply the snapshot then the batches to the
   * Y.Doc as apply-origin foreign updates, BEFORE the editor is mounted (SP-2 point 1).
   * Returns whether the document was brand-new (nothing to apply), which is the reliable
   * seed signal: the editor's own empty-doc sync can make the fragment non-empty before a
   * post-mount emptiness check runs, so seeding keys off what persistence pulled instead.
   */
  async hydrate(): Promise<{ readonly wasEmpty: boolean }> {
    this.#setStatus(this.#readOnly ? "read_only" : "hydrating");
    try {
      const pull = await this.#transport.pull({});
      if (this.#requireSnapshot && pull.snapshot === null) {
        throw new Error("Registered Co-work document has no canonical Y.Doc snapshot");
      }
      if (this.#hydrated && pull.ydocGeneration !== this.#ydocGeneration) {
        throw new CoworkYdocGenerationChangedError();
      }
      const incomingGeneration = pull.ydocGeneration;
      if (pull.snapshot !== null) {
        applyForeignUpdate(this.#doc, pull.snapshot);
      }
      for (const batch of pull.batches) {
        applyForeignUpdate(this.#doc, batch);
      }

      const recovered = this.#outbox === undefined ? [] : await this.#outbox.list();
      for (const entry of recovered) {
        if (
          !entry.acknowledged &&
          entry.generation !== incomingGeneration
        ) {
          // An unacknowledged update from another Y.Doc generation might encode text
          // removed by reimport. Its bytes remain in the outbox for recovery, but they must
          // never be applied automatically to this replacement snapshot.
          throw new CoworkYdocGenerationChangedError();
        }
        const existing = this.#pendingBatches.find(
          (pending) => pending.outboxId === entry.id,
        );
        if (entry.acknowledged) {
          // A complete pull already contains every acknowledged server update. Skipping it
          // also prevents an acknowledged row retained from a replaced generation from
          // contaminating the fresh Y.Doc before the next safe compaction prunes it.
          if (existing !== undefined) existing.acknowledged = true;
          continue;
        }
        // Replay the compatible device-local tail after canonical server state and before
        // the editor mounts. Reconcile by durable id so a StrictMode/retry hydration cannot
        // enqueue the same row for network retry twice.
        applyForeignUpdate(this.#doc, entry.batch);
        if (existing === undefined) {
          this.#pendingBatches.push(this.#pendingFromOutbox(entry));
        } else {
          existing.acknowledged = entry.acknowledged;
        }
      }
      this.#offset = pull.nextOffset;
      this.#docSha256 = pull.structuredHeadSha256 ?? pull.docSha256;
      this.#snapshotSha256 = pull.snapshotSha256;
      this.#ydocGeneration = incomingGeneration;
      this.#hydrated = true;
      this.#setStatus(
        this.#readOnly
          ? "read_only"
          : this.pendingBatchCount > 0
            ? "saved_on_device"
            : "clean",
      );
      return {
        wasEmpty:
          pull.snapshot === null && pull.batches.length === 0 && recovered.length === 0,
      };
    } catch (error) {
      this.#lastError = error;
      this.#setStatus(this.#classifyFailure(error));
      throw error;
    }
  }

  /** Begin pushing local human edits. Idempotent. */
  start(): void {
    if (this.#started) return;
    if (this.#readOnly) {
      this.#setStatus("read_only");
      return;
    }
    this.#started = true;
    this.#doc.on("update", this.#onUpdate);
    if (this.pendingBatchCount > 0) {
      void this.retry().catch(() => undefined);
    }
  }

  /** Stop pushing local human edits. Idempotent. */
  stop(): void {
    if (!this.#started) return;
    this.#started = false;
    this.#doc.off("update", this.#onUpdate);
    this.#cancelScheduledDrain();
  }

  /**
   * Make every already-observed human update durable on this device. This deliberately
   * performs no transport push: session navigation and policy changes must not wait for
   * the network, but they may never discard the only replayable copy of an edit.
   */
  async ensureDeviceDurability(): Promise<void> {
    await this.#durabilityTail;
    if (this.#pendingDurabilityBatches.length > 0) {
      await this.#queueDurabilityDrain();
    } else {
      await this.#durabilityChain;
    }
    if (this.#durabilityError !== null) throw this.#durabilityError;
  }

  /** Apply a live permission change without recreating the Y.Doc or losing its outbox. */
  async setReadOnly(readOnly: boolean): Promise<void> {
    if (readOnly === this.#readOnly) return;
    const epoch = ++this.#policyEpoch;
    if (readOnly) {
      // Stop accepting updates synchronously, then preserve everything already captured.
      this.stop();
      this.#readOnly = true;
      await this.ensureDeviceDurability();
      if (epoch === this.#policyEpoch && this.#readOnly) this.#setStatus("read_only");
      return;
    }
    this.#readOnly = false;
    this.#setStatus(
      this.pendingBatchCount > 0 ? "saved_on_device" : "clean",
    );
    this.start();
  }

  /** Await every queued push, so tests observe a settled server state. */
  async flush(): Promise<void> {
    this.#cancelScheduledDrain();
    await this.#durabilityChain;
    await this.#enqueue(() => this.#drainPending());
    if (this.#lastError !== null) throw this.#lastError;
  }

  /** Retry every unacknowledged local batch without requiring another edit. */
  async retry(): Promise<void> {
    this.#cancelScheduledDrain();
    await this.ensureDeviceDurability();
    if (this.#readOnly) {
      this.#setStatus("read_only");
      return;
    }
    this.#setStatus("retrying");
    return this.#enqueue(() => this.#drainPending());
  }

  /** Stop observing edits and wait only for device-local durability. */
  async dispose(): Promise<void> {
    this.stop();
    await this.ensureDeviceDurability();
    this.#statusListeners.clear();
  }

  /** Offset-sliced pull: apply only the batches appended after the client's cursor. */
  async pullSince(): Promise<void> {
    const pull = await this.#transport.pull({ sinceOffset: this.#offset });
    if (pull.ydocGeneration !== this.#ydocGeneration) {
      throw new CoworkYdocGenerationChangedError();
    }
    if (pull.cursorReset === true && pull.snapshot === null) {
      throw new Error("Y.Doc cursor reset response omitted its canonical snapshot");
    }
    // Ordinary compaction rotates the opaque log cursor but preserves the logical Y.Doc
    // generation. Its full snapshot is therefore safe to merge into the live Y.Doc.
    if (pull.snapshot !== null) {
      applyForeignUpdate(this.#doc, pull.snapshot);
      this.#snapshotSha256 = pull.snapshotSha256;
    }
    for (const batch of pull.batches) {
      applyForeignUpdate(this.#doc, batch);
    }
    this.#offset = pull.nextOffset;
    this.#docSha256 = pull.structuredHeadSha256 ?? pull.docSha256;
  }

  /**
   * Client-driven compaction: encode the whole doc state as one snapshot blob and push
   * it as a compaction rider, so the server content-addresses it and truncates the
   * superseded update log (section 1.4). The client owns all Yjs computation (C3).
   *
   * Compaction runs ON the same serialization chain as the human-edit pushes, so it can
   * never interleave with a queued push and leave `#docSha256` and `#offset` in a torn
   * state (S3). The returned promise settles when this compaction has run.
   */
  async compact(): Promise<CoworkCompactionReceipt> {
    await this.#enqueue(async () => {
      await this.#drainPending();
      await this.#compactOnce();
      if (this.#outbox !== undefined) {
        await this.#outbox.pruneAcknowledged();
        for (let index = this.#pendingBatches.length - 1; index >= 0; index -= 1) {
          if (this.#pendingBatches[index].acknowledged) {
            this.#pendingBatches.splice(index, 1);
          }
        }
      }
      this.#setStatus("clean");
    });
    if (this.#snapshotSha256 === null || this.#docSha256.length === 0) {
      throw new Error("Y.Doc compaction did not return a canonical structured head");
    }
    return {
      snapshotSha256: this.#snapshotSha256,
      structuredHeadSha256: this.#docSha256,
    };
  }

  async #compactOnce(): Promise<void> {
    const snapshot = Y.encodeStateAsUpdate(this.#doc);
    const snapshotSha256 = await sha256Hex(snapshot);
    const result = await this.#transport.push({
      batch: snapshot,
      baseSha256: this.#docSha256,
      baseStructuredHeadSha256: this.#docSha256,
      baseYdocGeneration: this.#ydocGeneration,
      compaction: { snapshot, snapshotSha256 },
    });
    if (result.ok) {
      this.#assertGeneration(result.ydocGeneration);
      this.#offset = result.nextOffset;
      this.#docSha256 = result.structuredHeadSha256 ?? result.docSha256;
      this.#snapshotSha256 = snapshotSha256;
      return;
    }
    // Another writer advanced the server, so catch up and compact once more against it.
    await this.pullSince();
    const retrySnapshot = Y.encodeStateAsUpdate(this.#doc);
    const retrySnapshotSha256 = await sha256Hex(retrySnapshot);
    const retry = await this.#transport.push({
      batch: retrySnapshot,
      baseSha256: this.#docSha256,
      baseStructuredHeadSha256: this.#docSha256,
      baseYdocGeneration: this.#ydocGeneration,
      compaction: {
        snapshot: retrySnapshot,
        snapshotSha256: retrySnapshotSha256,
      },
    });
    if (retry.ok) {
      this.#assertGeneration(retry.ydocGeneration);
      this.#offset = retry.nextOffset;
      this.#docSha256 = retry.structuredHeadSha256 ?? retry.docSha256;
      this.#snapshotSha256 = retrySnapshotSha256;
      return;
    }
    throw new Error("Y.Doc compaction remained stale after refreshing the server head");
  }

  readonly #onUpdate = (update: Uint8Array, origin: unknown): void => {
    // R4 carries HUMAN DIRECT EDITS ONLY. Apply-origin updates (proposal ingestion,
    // accepts, pulled batches) are never pushed (section 1.4 apply-origin discipline).
    if (!isLocalHumanOrigin(origin)) return;
    const batch = new Uint8Array(update);
    this.#setStatus("saving");
    if (this.#outbox === undefined) {
      this.#pendingBatches.push({ batch, acknowledged: false });
      this.#scheduleDrain();
      return;
    }

    // Keep the bytes in memory until the outbox confirms its write. A rejected append must
    // never drop the only replayable copy or be converted into a fulfilled durability chain.
    this.#pendingDurabilityBatches.push(batch);
    void this.#queueDurabilityDrain().catch(() => undefined);
  };

  #queueDurabilityDrain(): Promise<void> {
    if (this.#outbox === undefined) return Promise.resolve();
    const run = this.#durabilityTail.then(async () => {
      while (this.#pendingDurabilityBatches.length > 0) {
        const batch = this.#pendingDurabilityBatches[0];
        const entry = await this.#outbox?.append(
          batch,
          this.#ydocGeneration,
        );
        if (entry === undefined) {
          throw new Error("Co-work outbox did not persist the local update");
        }
        this.#pendingDurabilityBatches.shift();
        this.#pendingBatches.push(this.#pendingFromOutbox(entry));
      }
      this.#durabilityError = null;
      this.#setStatus(this.#readOnly ? "read_only" : "saved_on_device");
      if (this.#started && !this.#readOnly) this.#scheduleDrain();
    });
    this.#durabilityChain = run;
    this.#durabilityTail = run.then(
      () => undefined,
      (error: unknown) => {
        this.#durabilityError = error;
        this.#lastError = error;
        this.#setStatus("error");
      },
    );
    return run;
  }

  async #drainPending(): Promise<void> {
    if (this.#readOnly) {
      this.#setStatus("read_only");
      return;
    }
    for (;;) {
      const pending = this.#pendingBatches.filter((entry) => !entry.acknowledged);
      if (pending.length === 0) break;
      // A sentence typed quickly is one semantic local edit burst, not one HTTP request per
      // key. Yjs updates are safely mergeable and each original remains durable in the outbox
      // until this combined request is acknowledged.
      const batch =
        pending.length === 1
          ? pending[0].batch
          : Y.mergeUpdates(pending.map((entry) => entry.batch));
      this.#setStatus("saving");
      await this.#pushOnce(batch);
      for (const entry of pending) {
        if (this.#outbox !== undefined && entry.outboxId !== undefined) {
          await this.#outbox.acknowledge(entry.outboxId);
          entry.acknowledged = true;
        } else {
          const index = this.#pendingBatches.indexOf(entry);
          if (index >= 0) this.#pendingBatches.splice(index, 1);
        }
      }
    }
    if (this.#durabilityError !== null) throw this.#durabilityError;
    if (this.#pendingDurabilityBatches.length > 0) {
      this.#setStatus("saving");
      return;
    }
    this.#setStatus("clean");
  }

  #scheduleDrain(): void {
    if (!this.#started || this.#readOnly) return;
    this.#cancelScheduledDrain();
    this.#drainTimer = setTimeout(() => {
      this.#drainTimer = null;
      void this.#enqueue(() => this.#drainPending()).catch(() => undefined);
    }, NETWORK_BATCH_IDLE_MS);
  }

  #cancelScheduledDrain(): void {
    if (this.#drainTimer === null) return;
    clearTimeout(this.#drainTimer);
    this.#drainTimer = null;
  }

  async #pushOnce(batch: Uint8Array, isRetry = false): Promise<void> {
    const result = await this.#transport.push({
      batch,
      baseSha256: this.#docSha256,
      baseStructuredHeadSha256: this.#docSha256,
      baseYdocGeneration: this.#ydocGeneration,
    });
    if (result.ok) {
      this.#assertGeneration(result.ydocGeneration);
      this.#offset = result.nextOffset;
      this.#docSha256 = result.structuredHeadSha256 ?? result.docSha256;
      return;
    }
    if (isRetry) {
      throw new Error("Y.Doc update remained stale after refreshing the server head");
    }
    // Stale base: pull the missed remote batches, then re-push this batch once.
    await this.pullSince();
    await this.#pushOnce(batch, true);
  }

  #assertGeneration(generation: string): void {
    if (generation !== this.#ydocGeneration) {
      throw new CoworkYdocGenerationChangedError();
    }
  }

  #enqueue(operation: () => Promise<void>): Promise<void> {
    const run = this.#chain.then(operation);
    this.#chain = run.then(
      () => {
        if (
          this.#durabilityError === null &&
          this.#pendingDurabilityBatches.length === 0
        ) {
          this.#lastError = null;
        }
      },
      (error: unknown) => {
        this.#lastError = error;
        this.#setStatus(this.#classifyFailure(error));
      },
    );
    return run;
  }

  #pendingFromOutbox(entry: CoworkYdocOutboxEntry): PendingBatch {
    return {
      batch: new Uint8Array(entry.batch),
      outboxId: entry.id,
      acknowledged: entry.acknowledged,
    };
  }

  #setStatus(status: CoworkSyncStatus): void {
    if (status === this.#status) return;
    this.#status = status;
    for (const listener of this.#statusListeners) listener(status);
  }

  #classifyFailure(error: unknown): CoworkSyncStatus {
    if (error instanceof CoworkYdocGenerationChangedError) return "conflict";
    const message = error instanceof Error ? error.message.toLowerCase() : "";
    if (message.includes("stale") || message.includes("conflict")) return "conflict";
    if (
      error instanceof TypeError ||
      message.includes("offline") ||
      message.includes("network") ||
      message.includes("fetch")
    ) {
      return "offline";
    }
    return "error";
  }
}
