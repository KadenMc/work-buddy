/**
 * A local, reload-surviving realization of the Yjs transport seam (R3 pull / R4 push,
 * section 1.4, C3 opaque blobs). It holds exactly the opaque model the in-memory double
 * models, a compacted snapshot blob plus an append log of update batches plus a
 * `docSha256` content fingerprint, but reads that model from and writes it back to a
 * per-document backing store, so the blobs survive a page reload. Like the in-memory and
 * HTTP realizations it NEVER interprets, merges, or diffs the Yjs bytes.
 *
 * The backing is IndexedDB when it is available, and a process-memory store otherwise, so
 * jsdom and any storage-blocked context still get a working transport rather than a
 * throw. IndexedDB stores the `Uint8Array` blobs natively, which avoids the localStorage
 * size cap and the base64 inflation a growing binary CRDT would suffer there. The backing
 * is keyed per document id, so two documents never share state and a second transport on
 * the same id sees the first transport's persisted work.
 *
 * The transport is the source of truth for nothing it holds in memory: every pull and
 * push loads the current state from the backing first, so a fresh transport constructed
 * after a reload reads back exactly what an earlier one persisted under the same key. A
 * private serialization chain keeps each load-modify-store atomic against the next
 * operation on the same instance.
 */

import { frameSegments } from "./framing";
import { sha256Hex } from "./hashing";
import type {
  CoworkYdocPull,
  CoworkYdocPullRequest,
  CoworkYdocPushRequest,
  CoworkYdocPushResult,
  CoworkYdocTransport,
} from "./transport";

const EMPTY = new Uint8Array(0);

const STORAGE_KEY_PREFIX = "wb.cowork.ydoc.";
const DATABASE_NAME = "work-buddy-cowork";
const STORE_NAME = "cowork-ydoc";

/** The opaque persisted form of one document's server-equivalent state. */
export interface PersistedCoworkYdocState {
  /** The latest compacted snapshot blob, or null before the first compaction. */
  readonly snapshot: Uint8Array | null;
  /** Content hash of the snapshot blob when one is stored. */
  readonly snapshotSha256: string | null;
  /** Update batches appended after the snapshot, in append order. */
  readonly log: readonly Uint8Array[];
  /** Absolute offset of the first entry currently in `log` (advances on compaction). */
  readonly baseOffset: number;
}

/**
 * A per-document backing the transport loads before, and stores after, each mutation. It
 * moves whole opaque state records by key and never inspects their bytes.
 */
export interface CoworkYdocBackingStore {
  read(key: string): Promise<PersistedCoworkYdocState | undefined>;
  write(key: string, state: PersistedCoworkYdocState): Promise<void>;
}

/** Produces the backing store one transport instance uses. Injectable for tests. */
export type CoworkYdocBackingStoreFactory = () => CoworkYdocBackingStore;

export interface LocalCoworkYdocTransportOptions {
  readonly documentId: string;
  /** Injectable for tests, else IndexedDB with a process-memory fallback. */
  readonly factory?: CoworkYdocBackingStoreFactory;
}

const cloneBytes = (bytes: Uint8Array | null): Uint8Array | null =>
  bytes === null ? null : new Uint8Array(bytes);

const cloneState = (state: PersistedCoworkYdocState): PersistedCoworkYdocState => ({
  snapshot: cloneBytes(state.snapshot),
  snapshotSha256: state.snapshotSha256,
  log: state.log.map((batch) => new Uint8Array(batch)),
  baseOffset: state.baseOffset,
});

const emptyState = (): PersistedCoworkYdocState => ({
  snapshot: null,
  snapshotSha256: null,
  log: [],
  baseOffset: 0,
});

/**
 * The process-memory backing used when IndexedDB is unavailable, and the injectable
 * double the transport tests share between two instances to model a reload. It deep
 * copies on read and write, so its isolation matches the structured-clone semantics
 * IndexedDB gives for free.
 */
export class InMemoryCoworkYdocBackingStore implements CoworkYdocBackingStore {
  readonly #records = new Map<string, PersistedCoworkYdocState>();

  async read(key: string): Promise<PersistedCoworkYdocState | undefined> {
    const record = this.#records.get(key);
    return record === undefined ? undefined : cloneState(record);
  }

  async write(key: string, state: PersistedCoworkYdocState): Promise<void> {
    this.#records.set(key, cloneState(state));
  }
}

const requestResult = <Value>(request: IDBRequest<Value>): Promise<Value> =>
  new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(request.error ?? new Error("IndexedDB request failed"));
  });

const transactionDone = (transaction: IDBTransaction): Promise<void> =>
  new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () =>
      reject(transaction.error ?? new Error("IndexedDB transaction failed"));
    transaction.onabort = () =>
      reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
  });

interface StoredRecord extends PersistedCoworkYdocState {
  readonly key: string;
}

/**
 * The IndexedDB backing. It stores one record per document key holding the snapshot
 * blob, the append log, and the base offset as structured-cloned binary, so the blobs
 * survive a reload. The object store is lazily opened once per instance, mirroring the
 * widget-drafts repository that already runs IndexedDB for host drafts.
 */
export class IndexedDbCoworkYdocBackingStore implements CoworkYdocBackingStore {
  readonly #databaseName: string;
  #database?: Promise<IDBDatabase>;

  constructor(databaseName = DATABASE_NAME) {
    this.#databaseName = databaseName;
  }

  async read(key: string): Promise<PersistedCoworkYdocState | undefined> {
    const database = await this.#open();
    const transaction = database.transaction(STORE_NAME, "readonly");
    const record = (await requestResult(
      transaction.objectStore(STORE_NAME).get(key),
    )) as StoredRecord | undefined;
    await transactionDone(transaction);
    if (record === undefined) return undefined;
    return {
      snapshot: record.snapshot,
      snapshotSha256: record.snapshotSha256,
      log: record.log,
      baseOffset: record.baseOffset,
    };
  }

  async write(key: string, state: PersistedCoworkYdocState): Promise<void> {
    const database = await this.#open();
    const transaction = database.transaction(STORE_NAME, "readwrite");
    const record: StoredRecord = {
      key,
      snapshot: state.snapshot,
      snapshotSha256: state.snapshotSha256,
      log: [...state.log],
      baseOffset: state.baseOffset,
    };
    await requestResult(transaction.objectStore(STORE_NAME).put(record));
    await transactionDone(transaction);
  }

  #open(): Promise<IDBDatabase> {
    if (this.#database !== undefined) return this.#database;
    this.#database = new Promise((resolve, reject) => {
      const request = globalThis.indexedDB.open(this.#databaseName, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE_NAME)) {
          request.result.createObjectStore(STORE_NAME, { keyPath: "key" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () =>
        reject(request.error ?? new Error("Could not open cowork ydoc storage"));
    });
    return this.#database;
  }
}

/** IndexedDB when the runtime provides it, else the process-memory fallback. */
const defaultBackingStore = (): CoworkYdocBackingStore =>
  typeof indexedDB === "undefined"
    ? new InMemoryCoworkYdocBackingStore()
    : new IndexedDbCoworkYdocBackingStore();

export class LocalCoworkYdocTransport implements CoworkYdocTransport {
  readonly #key: string;
  readonly #backing: CoworkYdocBackingStore;
  #chain: Promise<unknown> = Promise.resolve();

  constructor(options: LocalCoworkYdocTransportOptions) {
    this.#key = `${STORAGE_KEY_PREFIX}${options.documentId}`;
    this.#backing = (options.factory ?? defaultBackingStore)();
  }

  pull(request: CoworkYdocPullRequest): Promise<CoworkYdocPull> {
    return this.#enqueue(() => this.#pull(request));
  }

  push(request: CoworkYdocPushRequest): Promise<CoworkYdocPushResult> {
    return this.#enqueue(() => this.#push(request));
  }

  /**
   * Run each operation after the previous one settles, so a load-modify-store never
   * interleaves with the next push on the same instance. The caller still observes the
   * operation's own result or rejection, while the chain itself is kept non-rejecting so
   * one failure does not poison the operations behind it.
   */
  #enqueue<Value>(operation: () => Promise<Value>): Promise<Value> {
    const run = this.#chain.then(operation);
    this.#chain = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  async #load(): Promise<PersistedCoworkYdocState> {
    const stored = await this.#backing.read(this.#key);
    return stored === undefined ? emptyState() : stored;
  }

  async #fingerprint(state: PersistedCoworkYdocState): Promise<string> {
    return sha256Hex(frameSegments([state.snapshot ?? EMPTY, ...state.log]));
  }

  async #pull(request: CoworkYdocPullRequest): Promise<CoworkYdocPull> {
    const state = await this.#load();
    const docSha256 = await this.#fingerprint(state);
    const nextOffset = String(state.baseOffset + state.log.length);
    const fullBatches = (): Uint8Array[] =>
      state.log.map((batch) => new Uint8Array(batch));

    if (request.sinceOffset === undefined) {
      return {
        snapshot: cloneBytes(state.snapshot),
        snapshotSha256: state.snapshotSha256,
        batches: fullBatches(),
        docSha256,
        nextOffset,
      };
    }
    const since = Number(request.sinceOffset);
    if (!Number.isFinite(since) || since < state.baseOffset) {
      // The caller is behind the snapshot boundary and cannot be served incrementally,
      // so fall back to a full pull (snapshot plus the whole current log).
      return {
        snapshot: cloneBytes(state.snapshot),
        snapshotSha256: state.snapshotSha256,
        batches: fullBatches(),
        docSha256,
        nextOffset,
      };
    }
    const start = since - state.baseOffset;
    return {
      snapshot: null,
      snapshotSha256: null,
      batches: state.log.slice(start).map((batch) => new Uint8Array(batch)),
      docSha256,
      nextOffset,
    };
  }

  async #push(request: CoworkYdocPushRequest): Promise<CoworkYdocPushResult> {
    const state = await this.#load();
    const docSha256 = await this.#fingerprint(state);
    if (request.baseSha256 !== docSha256) {
      return { ok: false, error: "stale_base", serverDocSha256: docSha256 };
    }

    if (request.compaction !== undefined) {
      const recomputed = await sha256Hex(request.compaction.snapshot);
      if (recomputed !== request.compaction.snapshotSha256) {
        // The backing content-addresses the blob and verifies the declared digest.
        throw new Error("Compaction snapshot does not re-hash to its declared digest");
      }
    }

    const log = state.log.map((batch) => new Uint8Array(batch));
    log.push(new Uint8Array(request.batch));

    let nextState: PersistedCoworkYdocState;
    if (request.compaction !== undefined) {
      // The snapshot subsumes every batch, so the superseded log is truncated and the
      // base offset advances past all of it.
      nextState = {
        snapshot: new Uint8Array(request.compaction.snapshot),
        snapshotSha256: request.compaction.snapshotSha256,
        log: [],
        baseOffset: state.baseOffset + log.length,
      };
    } else {
      nextState = {
        snapshot: cloneBytes(state.snapshot),
        snapshotSha256: state.snapshotSha256,
        log,
        baseOffset: state.baseOffset,
      };
    }

    await this.#backing.write(this.#key, nextState);
    const nextDocSha256 = await this.#fingerprint(nextState);
    return {
      ok: true,
      applied: true,
      docSha256: nextDocSha256,
      nextOffset: String(nextState.baseOffset + nextState.log.length),
    };
  }
}
