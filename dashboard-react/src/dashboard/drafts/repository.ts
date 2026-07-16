import type { JsonValue } from "../contributions/contracts";
import {
  WidgetDraftConflictError,
  type SaveWidgetDraftRequest,
  type WidgetDraftEnvelope,
  type WidgetDraftIdentity,
  type WidgetDraftRepository,
  widgetDraftStorageKey,
} from "./contracts";

const DAY_MS = 24 * 60 * 60 * 1_000;

const clone = <Value>(value: Value): Value =>
  typeof structuredClone === "function"
    ? structuredClone(value)
    : (JSON.parse(JSON.stringify(value)) as Value);

const createEnvelope = (
  request: SaveWidgetDraftRequest,
  revision: number,
): WidgetDraftEnvelope => {
  const updatedAt = new Date().toISOString();
  return {
    envelopeVersion: 1,
    storageKey: widgetDraftStorageKey(request),
    profileId: request.profileId,
    workspaceId: request.workspaceId,
    appId: request.appId,
    viewId: request.viewId,
    instanceId: request.instanceId,
    widgetTypeId: request.widgetTypeId,
    draftName: request.draftName,
    scopeKey: request.scopeKey,
    draftSchema: request.draftSchema,
    revision,
    value: clone(request.value) as JsonValue,
    updatedAt,
    ...(request.retentionDays === undefined
      ? {}
      : {
          expiresAt: new Date(
            Date.parse(updatedAt) + request.retentionDays * DAY_MS,
          ).toISOString(),
        }),
  };
};

const isExpired = (draft: WidgetDraftEnvelope): boolean =>
  draft.expiresAt !== undefined && Date.parse(draft.expiresAt) <= Date.now();

export class InMemoryWidgetDraftRepository implements WidgetDraftRepository {
  readonly #drafts = new Map<string, WidgetDraftEnvelope>();
  readonly #listeners = new Set<(storageKey: string) => void>();

  async load(identity: WidgetDraftIdentity): Promise<WidgetDraftEnvelope | undefined> {
    const key = widgetDraftStorageKey(identity);
    const current = this.#drafts.get(key);
    if (current !== undefined && isExpired(current)) {
      this.#drafts.delete(key);
      return undefined;
    }
    return current === undefined ? undefined : clone(current);
  }

  async save(request: SaveWidgetDraftRequest): Promise<WidgetDraftEnvelope> {
    const key = widgetDraftStorageKey(request);
    const current = this.#drafts.get(key);
    if (request.expectedRevision !== current?.revision) {
      if (!(request.expectedRevision === undefined && current === undefined)) {
        throw new WidgetDraftConflictError(key);
      }
    }
    const next = createEnvelope(request, (current?.revision ?? 0) + 1);
    this.#drafts.set(key, next);
    this.#emit(key);
    return clone(next);
  }

  async delete(identity: WidgetDraftIdentity, expectedRevision?: number): Promise<void> {
    const key = widgetDraftStorageKey(identity);
    const current = this.#drafts.get(key);
    if (
      expectedRevision !== undefined &&
      current !== undefined &&
      current.revision !== expectedRevision
    ) {
      throw new WidgetDraftConflictError(key);
    }
    this.#drafts.delete(key);
    this.#emit(key);
  }

  subscribe(listener: (storageKey: string) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #emit(storageKey: string) {
    this.#listeners.forEach((listener) => listener(storageKey));
  }
}

/**
 * A disposable copy-on-write view over a real repository. Loads fall through to the
 * base, while saves and deletes remain in the fork for the lifetime of the instance.
 */
export class ForkedWidgetDraftRepository implements WidgetDraftRepository {
  readonly #drafts = new Map<string, WidgetDraftEnvelope>();
  readonly #deleted = new Set<string>();

  constructor(readonly base: WidgetDraftRepository) {}

  async load(identity: WidgetDraftIdentity): Promise<WidgetDraftEnvelope | undefined> {
    const key = widgetDraftStorageKey(identity);
    if (this.#deleted.has(key)) return undefined;
    const forked = this.#drafts.get(key);
    if (forked !== undefined) return clone(forked);
    return this.base.load(identity);
  }

  async save(request: SaveWidgetDraftRequest): Promise<WidgetDraftEnvelope> {
    const key = widgetDraftStorageKey(request);
    const current = await this.load(request);
    if (request.expectedRevision !== current?.revision) {
      if (!(request.expectedRevision === undefined && current === undefined)) {
        throw new WidgetDraftConflictError(key);
      }
    }
    const next = createEnvelope(request, (current?.revision ?? 0) + 1);
    this.#deleted.delete(key);
    this.#drafts.set(key, next);
    return clone(next);
  }

  async delete(identity: WidgetDraftIdentity, expectedRevision?: number): Promise<void> {
    const key = widgetDraftStorageKey(identity);
    const current = await this.load(identity);
    if (
      expectedRevision !== undefined &&
      current !== undefined &&
      current.revision !== expectedRevision
    ) {
      throw new WidgetDraftConflictError(key);
    }
    this.#drafts.delete(key);
    this.#deleted.add(key);
  }
}

const requestResult = <Value>(request: IDBRequest<Value>): Promise<Value> =>
  new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });

const transactionDone = (transaction: IDBTransaction): Promise<void> =>
  new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
  });

export class IndexedDbWidgetDraftRepository implements WidgetDraftRepository {
  readonly #databaseName: string;
  readonly #listeners = new Set<(storageKey: string) => void>();
  readonly #channel?: BroadcastChannel;
  #database?: Promise<IDBDatabase>;

  constructor(databaseName = "work-buddy-dashboard") {
    this.#databaseName = databaseName;
    if (typeof BroadcastChannel !== "undefined") {
      this.#channel = new BroadcastChannel(`${databaseName}:widget-drafts`);
      this.#channel.addEventListener("message", (event: MessageEvent<unknown>) => {
        if (typeof event.data === "string") this.#emit(event.data);
      });
    }
  }

  async load(identity: WidgetDraftIdentity): Promise<WidgetDraftEnvelope | undefined> {
    const key = widgetDraftStorageKey(identity);
    const database = await this.#open();
    const transaction = database.transaction("widget-drafts", "readonly");
    const current = (await requestResult(
      transaction.objectStore("widget-drafts").get(key),
    )) as WidgetDraftEnvelope | undefined;
    await transactionDone(transaction);
    if (current !== undefined && isExpired(current)) {
      await this.delete(identity, current.revision);
      return undefined;
    }
    return current;
  }

  async save(request: SaveWidgetDraftRequest): Promise<WidgetDraftEnvelope> {
    const key = widgetDraftStorageKey(request);
    const database = await this.#open();
    const transaction = database.transaction("widget-drafts", "readwrite");
    const store = transaction.objectStore("widget-drafts");
    const current = (await requestResult(store.get(key))) as WidgetDraftEnvelope | undefined;
    if (request.expectedRevision !== current?.revision) {
      if (!(request.expectedRevision === undefined && current === undefined)) {
        transaction.abort();
        throw new WidgetDraftConflictError(key);
      }
    }
    const next = createEnvelope(request, (current?.revision ?? 0) + 1);
    await requestResult(store.put(next));
    await transactionDone(transaction);
    this.#publish(key);
    return next;
  }

  async delete(identity: WidgetDraftIdentity, expectedRevision?: number): Promise<void> {
    const key = widgetDraftStorageKey(identity);
    const database = await this.#open();
    const transaction = database.transaction("widget-drafts", "readwrite");
    const store = transaction.objectStore("widget-drafts");
    const current = (await requestResult(store.get(key))) as WidgetDraftEnvelope | undefined;
    if (
      expectedRevision !== undefined &&
      current !== undefined &&
      current.revision !== expectedRevision
    ) {
      transaction.abort();
      throw new WidgetDraftConflictError(key);
    }
    await requestResult(store.delete(key));
    await transactionDone(transaction);
    this.#publish(key);
  }

  subscribe(listener: (storageKey: string) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #publish(storageKey: string) {
    this.#emit(storageKey);
    this.#channel?.postMessage(storageKey);
  }

  #emit(storageKey: string) {
    this.#listeners.forEach((listener) => listener(storageKey));
  }

  #open(): Promise<IDBDatabase> {
    if (this.#database !== undefined) return this.#database;
    this.#database = new Promise((resolve, reject) => {
      const request = indexedDB.open(this.#databaseName, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains("widget-drafts")) {
          request.result.createObjectStore("widget-drafts", { keyPath: "storageKey" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error ?? new Error("Could not open draft storage"));
    });
    return this.#database;
  }
}

export const createBrowserWidgetDraftRepository = (): WidgetDraftRepository =>
  typeof indexedDB === "undefined"
    ? new InMemoryWidgetDraftRepository()
    : new IndexedDbWidgetDraftRepository();
