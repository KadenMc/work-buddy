/**
 * Device-local replay log for registered Co-work Y.Doc updates.
 *
 * Entries are retained after the server acknowledges them. They are pruned
 * only after a later acknowledged compaction proves the complete local state
 * (and therefore every acknowledged entry) is present in a durable snapshot.
 */

const DATABASE_NAME = "work-buddy-cowork-outbox";
const STORE_NAME = "ydoc-outbox";

export interface CoworkYdocOutboxEntry {
  readonly id: number;
  readonly batch: Uint8Array;
  readonly acknowledged: boolean;
  /** Snapshot generation against which this local update was authored. */
  readonly generation?: string;
}

export interface CoworkYdocOutbox {
  list(): Promise<readonly CoworkYdocOutboxEntry[]>;
  append(batch: Uint8Array, generation?: string): Promise<CoworkYdocOutboxEntry>;
  acknowledge(id: number): Promise<void>;
  pruneAcknowledged(): Promise<void>;
}

interface PersistedOutbox {
  readonly key: string;
  readonly nextId: number;
  readonly entries: readonly CoworkYdocOutboxEntry[];
}

export interface CoworkYdocOutboxBackingStore {
  read(key: string): Promise<PersistedOutbox | undefined>;
  append(
    key: string,
    batch: Uint8Array,
    generation?: string,
  ): Promise<CoworkYdocOutboxEntry>;
  acknowledge(key: string, id: number): Promise<void>;
  pruneAcknowledged(key: string): Promise<void>;
}

const cloneEntry = (entry: CoworkYdocOutboxEntry): CoworkYdocOutboxEntry => ({
  id: entry.id,
  batch: new Uint8Array(entry.batch),
  acknowledged: entry.acknowledged,
  ...(entry.generation === undefined ? {} : { generation: entry.generation }),
});

const cloneRecord = (record: PersistedOutbox): PersistedOutbox => ({
  key: record.key,
  nextId: record.nextId,
  entries: record.entries.map(cloneEntry),
});

export class InMemoryCoworkYdocOutboxBackingStore
  implements CoworkYdocOutboxBackingStore
{
  readonly #records = new Map<string, PersistedOutbox>();

  async read(key: string): Promise<PersistedOutbox | undefined> {
    const value = this.#records.get(key);
    return value === undefined ? undefined : cloneRecord(value);
  }

  async append(
    key: string,
    batch: Uint8Array,
    generation?: string,
  ): Promise<CoworkYdocOutboxEntry> {
    const current = this.#record(key);
    const entry: CoworkYdocOutboxEntry = {
      id: current.nextId,
      batch: new Uint8Array(batch),
      acknowledged: false,
      ...(generation === undefined ? {} : { generation }),
    };
    this.#records.set(key, {
      key,
      nextId: current.nextId + 1,
      entries: [...current.entries, entry],
    });
    return cloneEntry(entry);
  }

  async acknowledge(key: string, id: number): Promise<void> {
    const current = this.#record(key);
    this.#records.set(key, {
      ...current,
      entries: current.entries.map((entry) =>
        entry.id === id ? { ...entry, acknowledged: true } : entry,
      ),
    });
  }

  async pruneAcknowledged(key: string): Promise<void> {
    const current = this.#record(key);
    this.#records.set(key, {
      ...current,
      entries: current.entries.filter((entry) => !entry.acknowledged),
    });
  }

  #record(key: string): PersistedOutbox {
    return (
      this.#records.get(key) ?? {
        key,
        nextId: 1,
        entries: [],
      }
    );
  }
}

const requestResult = <Value>(request: IDBRequest<Value>): Promise<Value> =>
  new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(request.error ?? new Error("IndexedDB outbox request failed"));
  });

const transactionDone = (transaction: IDBTransaction): Promise<void> =>
  new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () =>
      reject(transaction.error ?? new Error("IndexedDB outbox transaction failed"));
    transaction.onabort = () =>
      reject(transaction.error ?? new Error("IndexedDB outbox transaction aborted"));
  });

export class IndexedDbCoworkYdocOutboxBackingStore
  implements CoworkYdocOutboxBackingStore
{
  readonly #databaseName: string;
  #database?: Promise<IDBDatabase>;

  constructor(databaseName = DATABASE_NAME) {
    this.#databaseName = databaseName;
  }

  async read(key: string): Promise<PersistedOutbox | undefined> {
    const database = await this.#open();
    const transaction = database.transaction(STORE_NAME, "readonly");
    const done = transactionDone(transaction);
    const value = (await requestResult(
      transaction.objectStore(STORE_NAME).get(key),
    )) as PersistedOutbox | undefined;
    await done;
    return value === undefined ? undefined : cloneRecord(value);
  }

  async append(
    key: string,
    batch: Uint8Array,
    generation?: string,
  ): Promise<CoworkYdocOutboxEntry> {
    return this.#mutate(key, (current) => {
      const entry: CoworkYdocOutboxEntry = {
        id: current.nextId,
        batch: new Uint8Array(batch),
        acknowledged: false,
        ...(generation === undefined ? {} : { generation }),
      };
      return {
        record: {
          key,
          nextId: current.nextId + 1,
          entries: [...current.entries, entry],
        },
        result: cloneEntry(entry),
      };
    });
  }

  async acknowledge(key: string, id: number): Promise<void> {
    await this.#mutate(key, (current) => ({
      record: {
        ...current,
        entries: current.entries.map((entry) =>
          entry.id === id ? { ...entry, acknowledged: true } : entry,
        ),
      },
      result: undefined,
    }));
  }

  async pruneAcknowledged(key: string): Promise<void> {
    await this.#mutate(key, (current) => ({
      record: {
        ...current,
        entries: current.entries.filter((entry) => !entry.acknowledged),
      },
      result: undefined,
    }));
  }

  /**
   * IndexedDB serializes read-write transactions that touch the same object store,
   * including transactions opened by another tab. Keeping the get and put in this one
   * transaction makes the persisted record the serialization boundary: no two tabs can
   * allocate the same id or replace a newer append/ack with a stale whole-record write.
   */
  async #mutate<Value>(
    key: string,
    mutation: (
      current: PersistedOutbox,
    ) => { readonly record: PersistedOutbox; readonly result: Value },
  ): Promise<Value> {
    const database = await this.#open();
    const transaction = database.transaction(STORE_NAME, "readwrite");
    const done = transactionDone(transaction);
    const store = transaction.objectStore(STORE_NAME);
    try {
      const stored = (await requestResult(
        store.get(key),
      )) as PersistedOutbox | undefined;
      const current: PersistedOutbox =
        stored === undefined
          ? { key, nextId: 1, entries: [] }
          : cloneRecord(stored);
      const { record, result } = mutation(current);
      if (record.key !== key) {
        transaction.abort();
        await done.catch(() => undefined);
        throw new Error("Co-work outbox key does not match its persisted record");
      }
      store.put(cloneRecord(record));
      await done;
      return result;
    } catch (error) {
      try {
        transaction.abort();
      } catch {
        // The transaction may already have completed or aborted after a request error.
      }
      await done.catch(() => undefined);
      throw error;
    }
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
        reject(request.error ?? new Error("Could not open Co-work outbox"));
    });
    return this.#database;
  }
}

const fallbackBacking = new InMemoryCoworkYdocOutboxBackingStore();

export class DurableCoworkYdocOutbox implements CoworkYdocOutbox {
  readonly #key: string;
  readonly #backing: CoworkYdocOutboxBackingStore;
  #chain: Promise<unknown> = Promise.resolve();

  constructor(
    key: string,
    backing: CoworkYdocOutboxBackingStore =
      typeof indexedDB === "undefined"
        ? fallbackBacking
        : new IndexedDbCoworkYdocOutboxBackingStore(),
  ) {
    this.#key = key;
    this.#backing = backing;
  }

  list(): Promise<readonly CoworkYdocOutboxEntry[]> {
    return this.#enqueue(async () => {
      const record = await this.#read();
      return record.entries.map(cloneEntry);
    });
  }

  append(
    batch: Uint8Array,
    generation?: string,
  ): Promise<CoworkYdocOutboxEntry> {
    return this.#enqueue(() =>
      this.#backing.append(this.#key, batch, generation),
    );
  }

  acknowledge(id: number): Promise<void> {
    return this.#enqueue(() => this.#backing.acknowledge(this.#key, id));
  }

  pruneAcknowledged(): Promise<void> {
    return this.#enqueue(() => this.#backing.pruneAcknowledged(this.#key));
  }

  async #read(): Promise<PersistedOutbox> {
    return (
      (await this.#backing.read(this.#key)) ?? {
        key: this.#key,
        nextId: 1,
        entries: [],
      }
    );
  }

  #enqueue<Value>(operation: () => Promise<Value>): Promise<Value> {
    const run = this.#chain.then(operation);
    this.#chain = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }
}
