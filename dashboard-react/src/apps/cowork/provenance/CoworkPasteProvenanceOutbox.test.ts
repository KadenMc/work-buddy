import { beforeEach, describe, expect, it } from "vitest";

import {
  defaultCoworkProvenanceDetermination,
  unknownCoworkProvenanceDetermination,
} from "./contracts";
import {
  CoworkPasteProvenanceExactLimitError,
  DurableCoworkPasteProvenanceOutbox,
  IndexedDbCoworkPasteProvenanceOutboxBackingStore,
  InMemoryCoworkPasteProvenanceIntentStage,
  InMemoryCoworkPasteProvenanceOutboxBackingStore,
  WebStorageCoworkPasteProvenanceIntentStage,
  type CoworkPasteProvenanceOutboxBackingStore,
  type CoworkPasteProvenanceStorageWarning,
} from "./CoworkPasteProvenanceOutbox";
import { COWORK_PROVENANCE_EXACT_MAX_CHARS } from "./pasteProvenance";

const actorIdentity = {
  kind: "human",
  ref: "local:test-actor",
  identity_status: "local_actor_ref",
} as const;
const humanDetermination = () =>
  defaultCoworkProvenanceDetermination(actorIdentity);

const anchor = {
  exact: "pasted text",
  prefix: "before ",
  suffix: " after",
};

class MemoryStorage implements Storage {
  readonly #values = new Map<string, string>();

  get length(): number {
    return this.#values.size;
  }

  clear(): void {
    this.#values.clear();
  }

  getItem(key: string): string | null {
    return this.#values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.#values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.#values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.#values.set(key, value);
  }

  keys(): readonly string[] {
    return [...this.#values.keys()];
  }
}

const fakeIndexedDatabase = (records: Map<string, unknown>): IDBDatabase =>
  ({
    transaction: () => {
      let aborted = false;
      const transaction = {
        error: null,
        oncomplete: null,
        onerror: null,
        onabort: null,
      } as unknown as IDBTransaction & {
        oncomplete: ((event: Event) => void) | null;
        onabort: ((event: Event) => void) | null;
      };
      const store = {
        get: (key: IDBValidKey) => {
          const request = {
            result: undefined,
            error: null,
            onsuccess: null,
            onerror: null,
          } as unknown as IDBRequest<unknown> & {
            result: unknown;
            onsuccess: ((event: Event) => void) | null;
          };
          queueMicrotask(() => {
            request.result = records.get(String(key));
            request.onsuccess?.(new Event("success"));
            setTimeout(() => {
              if (!aborted) {
                transaction.oncomplete?.(new Event("complete"));
              }
            }, 0);
          });
          return request;
        },
        put: (value: { readonly key: string }) => {
          records.set(value.key, value);
          return {} as IDBRequest<IDBValidKey>;
        },
      } as unknown as IDBObjectStore;
      Object.assign(transaction, {
        objectStore: () => store,
        abort: () => {
          aborted = true;
          queueMicrotask(() => {
            transaction.onabort?.(new Event("abort"));
          });
        },
      });
      return transaction;
    },
  }) as unknown as IDBDatabase;

describe("DurableCoworkPasteProvenanceOutbox", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("rehydrates unresolved entries in capture order and isolates documents", async () => {
    const backing = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    const first = new DurableCoworkPasteProvenanceOutbox(
      "store:document",
      backing,
    );
    await first.append({
      anchor,
      idempotencyKey: "paste-1",
      substantial: true,
      basisKind: "user_attestation",
      determination: unknownCoworkProvenanceDetermination(),
      status: "awaiting_determination",
    });
    await first.append({
      anchor: { ...anchor, exact: "second paste" },
      idempotencyKey: "paste-2",
      substantial: false,
      basisKind: "automatic_short_text_attribution",
      determination: humanDetermination(),
      status: "ready",
    });

    const reopened = new DurableCoworkPasteProvenanceOutbox(
      "store:document",
      backing,
    );
    expect(
      (await reopened.list()).map((entry) => entry.idempotencyKey),
    ).toEqual(["paste-1", "paste-2"]);
    expect(
      await new DurableCoworkPasteProvenanceOutbox(
        "store:other",
        backing,
      ).list(),
    ).toEqual([]);
  });

  it("freezes the first complete request for ambiguous retries", async () => {
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      "store:document",
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const entry = await outbox.append({
      anchor,
      idempotencyKey: "stable-key",
      substantial: false,
      basisKind: "automatic_short_text_attribution",
      determination: humanDetermination(),
      status: "ready",
    });
    const first = await outbox.freezeRequest(entry.id, {
      storeId: "store",
      documentId: "document",
      expectedStructuredHeadSha256: "a".repeat(64),
    });
    const second = await outbox.freezeRequest(entry.id, {
      storeId: "store",
      documentId: "document",
      expectedStructuredHeadSha256: "b".repeat(64),
    });

    expect(second.frozenRequest).toEqual(first.frozenRequest);
    expect(second.frozenRequest).toMatchObject({
      idempotencyKey: "stable-key",
      expectedStructuredHeadSha256: "a".repeat(64),
    });
  });

  it("recovers the latest staged shape of an open typing burst", async () => {
    const key = "store:direct-recovery";
    const backing = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    const stage = new InMemoryCoworkPasteProvenanceIntentStage();
    const first = new DurableCoworkPasteProvenanceOutbox(key, backing, stage);
    await first.upsertCapture({
      anchor: { exact: "T", prefix: "", suffix: " after" },
      idempotencyKey: "typing-burst",
      substantial: false,
      capturedActor: actorIdentity,
      sourceKind: "direct_entry",
      basisKind: "automatic_direct_entry_attribution",
      determination: humanDetermination(),
      status: "capturing",
    });

    // Simulate a crash after the synchronous journal advanced to `Test` but
    // before the older durable `T` row was updated.
    stage.put(key, {
      anchor: { exact: "Test", prefix: "", suffix: " after" },
      idempotencyKey: "typing-burst",
      substantial: false,
      capturedActor: actorIdentity,
      sourceKind: "direct_entry",
      basisKind: "automatic_direct_entry_attribution",
      determination: humanDetermination(),
      status: "capturing",
    });

    const reopened = new DurableCoworkPasteProvenanceOutbox(
      key,
      backing,
      stage,
    );
    expect((await reopened.list())[0]).toMatchObject({
      id: 1,
      status: "capturing",
      anchor: { exact: "Test" },
    });
  });

  it("never lets a stale open-stage row overwrite a ready frozen request", async () => {
    const key = "store:frozen-stage";
    const backing = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    const stage = new InMemoryCoworkPasteProvenanceIntentStage();
    const outbox = new DurableCoworkPasteProvenanceOutbox(key, backing, stage);
    const open = await outbox.upsertCapture({
      anchor: { exact: "T", prefix: "", suffix: " after" },
      idempotencyKey: "typing-burst",
      substantial: false,
      capturedActor: actorIdentity,
      sourceKind: "direct_entry",
      basisKind: "automatic_direct_entry_attribution",
      determination: humanDetermination(),
      status: "capturing",
    });
    await outbox.markReady(
      open.id,
      open.determination,
      "automatic_direct_entry_attribution",
    );
    await outbox.freezeRequest(open.id, {
      storeId: "store",
      documentId: "document",
      expectedStructuredHeadSha256: "a".repeat(64),
    });
    stage.put(key, {
      anchor: { exact: "Test", prefix: "", suffix: " after" },
      idempotencyKey: "typing-burst",
      substantial: false,
      capturedActor: actorIdentity,
      sourceKind: "direct_entry",
      basisKind: "automatic_direct_entry_attribution",
      determination: humanDetermination(),
      status: "capturing",
    });

    const [recovered] = await new DurableCoworkPasteProvenanceOutbox(
      key,
      backing,
      stage,
    ).list();
    expect(recovered).toMatchObject({
      status: "ready",
      anchor: { exact: "T" },
      frozenRequest: {
        expectedActorRef: actorIdentity.ref,
        expectedActorIdentityStatus: actorIdentity.identity_status,
      },
    });
    await expect(
      outbox.updateDetermination(
        open.id,
        unknownCoworkProvenanceDetermination(),
      ),
    ).rejects.toThrow("frozen");
    await expect(
      outbox.markReady(
        open.id,
        unknownCoworkProvenanceDetermination(),
        "automatic_direct_entry_attribution",
      ),
    ).rejects.toThrow("frozen");
  });

  it("atomically requires fresh explicit determinations for every pending entry after an actor change", async () => {
    const backing = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      "store:actor-change",
      backing,
    );
    const first = await outbox.append({
      anchor,
      idempotencyKey: "old-automatic-key",
      substantial: false,
      basisKind: "automatic_short_text_attribution",
      determination: humanDetermination(),
      status: "ready",
    });
    await outbox.freezeRequest(first.id, {
      storeId: "store",
      documentId: "document",
      expectedStructuredHeadSha256: "a".repeat(64),
    });
    await outbox.append({
      anchor: { ...anchor, exact: "another paste" },
      idempotencyKey: "old-explicit-key",
      substantial: true,
      basisKind: "user_attestation",
      determination: humanDetermination(),
      status: "ready",
    });
    await outbox.upsertCapture({
      anchor: { ...anchor, exact: "typed text" },
      idempotencyKey: "old-direct-entry-key",
      substantial: false,
      capturedActor: actorIdentity,
      sourceKind: "direct_entry",
      basisKind: "automatic_direct_entry_attribution",
      determination: humanDetermination(),
      status: "capturing",
    });

    const reset = await outbox.resetAfterActorChange(
      "new-actor-attempt",
      unknownCoworkProvenanceDetermination(),
    );

    expect(reset).toHaveLength(3);
    expect(reset.map((entry) => entry.idempotencyKey)).toEqual([
      `new-actor-attempt:${String(first.id)}`,
      "new-actor-attempt:2",
      "new-actor-attempt:3",
    ]);
    for (const entry of reset) {
      expect(entry).toMatchObject({
        status: "awaiting_determination",
        basisKind: "user_attestation",
        requiresExplicitDetermination: true,
        determination: {
          authorship: { kind: "unknown", contributors: [] },
        },
        failure: { code: "provenance_actor_changed" },
      });
      expect(entry.frozenRequest).toBeUndefined();
    }
    expect(reset[2]).toMatchObject({
      sourceKind: "legacy",
      basisKind: "user_attestation",
      determination: { authorship: { kind: "unknown" } },
    });

    const edited = await outbox.updateDetermination(
      first.id,
      humanDetermination(),
    );
    expect(edited.requiresExplicitDetermination).toBe(true);
    expect(edited.failure?.code).toBe("provenance_actor_changed");
    const readied = await outbox.markReady(
      first.id,
      humanDetermination(),
      "user_attestation",
    );
    expect(readied.requiresExplicitDetermination).toBeUndefined();
    expect(readied.failure).toBeUndefined();

    const reopened = new DurableCoworkPasteProvenanceOutbox(
      "store:actor-change",
      backing,
    );
    expect((await reopened.list())[1]).toMatchObject({
      status: "awaiting_determination",
      requiresExplicitDetermination: true,
      failure: { code: "provenance_actor_changed" },
    });
  });

  it("rejects an oversized exact span before touching either recovery journal", async () => {
    const key = "store:oversized";
    const backing = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    const intentStage = new InMemoryCoworkPasteProvenanceIntentStage();
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      key,
      backing,
      intentStage,
    );

    await expect(
      outbox.append({
        anchor: {
          ...anchor,
          exact: "x".repeat(COWORK_PROVENANCE_EXACT_MAX_CHARS + 1),
        },
        idempotencyKey: "oversized-paste",
        substantial: true,
        basisKind: "user_attestation",
        determination: unknownCoworkProvenanceDetermination(),
        status: "awaiting_determination",
      }),
    ).rejects.toBeInstanceOf(CoworkPasteProvenanceExactLimitError);
    expect(intentStage.list(key)).toEqual([]);
    expect(await outbox.list()).toEqual([]);
  });

  it("rejects invisible capturing states for paste and legacy sources", async () => {
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      "store:invalid-capturing",
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
      new InMemoryCoworkPasteProvenanceIntentStage(),
    );
    await expect(
      outbox.upsertCapture({
        anchor,
        idempotencyKey: "capturing-paste",
        substantial: false,
        sourceKind: "paste",
        basisKind: "user_attestation",
        determination: humanDetermination(),
        status: "capturing",
      }),
    ).rejects.toThrow("automatic direct-entry");
    await expect(
      outbox.append({
        anchor,
        idempotencyKey: "capturing-legacy",
        substantial: false,
        sourceKind: "legacy",
        basisKind: "user_attestation",
        determination: humanDetermination(),
        status: "capturing",
      }),
    ).rejects.toThrow("invalid source, basis, or state");
    expect(await outbox.list()).toEqual([]);
  });

  it("retargets a stale request only through the explicit recovery operation", async () => {
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      "store:document",
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const entry = await outbox.append({
      anchor,
      idempotencyKey: "old-key",
      substantial: true,
      basisKind: "user_attestation",
      determination: unknownCoworkProvenanceDetermination(),
      status: "ready",
    });
    await outbox.freezeRequest(entry.id, {
      storeId: "store",
      documentId: "document",
      expectedStructuredHeadSha256: "a".repeat(64),
    });
    await outbox.markFailure(entry.id, {
      code: "stale_structured_head",
      message: "stale",
      kind: "stale_target",
    });

    const stale = (await outbox.list())[0];
    expect(stale).toMatchObject({
      status: "stale_target",
      idempotencyKey: "old-key",
      frozenRequest: {
        expectedStructuredHeadSha256: "a".repeat(64),
      },
    });

    const recovered = await outbox.retarget(
      entry.id,
      "new-key",
      humanDetermination(),
    );
    expect(recovered).toMatchObject({
      status: "ready",
      idempotencyKey: "new-key",
    });
    expect(recovered.frozenRequest).toBeUndefined();
  });

  it("deletes an entry only when explicitly given a confirmed receipt boundary", async () => {
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      "store:document",
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
    );
    const entry = await outbox.append({
      anchor,
      idempotencyKey: "pending",
      substantial: false,
      basisKind: "automatic_short_text_attribution",
      determination: humanDetermination(),
      status: "ready",
    });
    await outbox.markFailure(entry.id, {
      code: "network_error",
      message: "offline",
      kind: "retryable",
    });
    expect(await outbox.list()).toHaveLength(1);

    await outbox.remove(entry.id);
    expect(await outbox.list()).toEqual([]);
  });

  it("rehydrates a synchronously staged intent after the IndexedDB write fails", async () => {
    const backing = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
    const stage = new InMemoryCoworkPasteProvenanceIntentStage();
    let fail = true;
    const flaky: CoworkPasteProvenanceOutboxBackingStore = {
      durable: true,
      read: (key) => backing.read(key),
      mutate: (key, mutation) => {
        if (fail) {
          fail = false;
          return Promise.reject(new Error("quota"));
        }
        return backing.mutate(key, mutation);
      },
    };
    const first = new DurableCoworkPasteProvenanceOutbox(
      "store:staged",
      flaky,
      stage,
    );
    await expect(
      first.append({
        anchor,
        idempotencyKey: "staged-key",
        substantial: true,
        basisKind: "user_attestation",
        determination: unknownCoworkProvenanceDetermination(),
        status: "awaiting_determination",
      }),
    ).rejects.toThrow("quota");

    const reopened = new DurableCoworkPasteProvenanceOutbox(
      "store:staged",
      backing,
      stage,
    );
    expect(await reopened.list()).toMatchObject([
      {
        idempotencyKey: "staged-key",
        passageExcerpt: "pasted text",
        status: "awaiting_determination",
      },
    ]);
  });

  it("keeps the localStorage recovery journal until a volatile fallback receives a server receipt", async () => {
    const storage = new MemoryStorage();
    const key = "store:volatile-fallback";
    const prefix = "test-volatile-stage:";
    const stage = new WebStorageCoworkPasteProvenanceIntentStage(
      storage,
      prefix,
    );
    const first = new DurableCoworkPasteProvenanceOutbox(
      key,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
      stage,
    );
    await first.append({
      anchor,
      idempotencyKey: "volatile-paste",
      substantial: true,
      basisKind: "user_attestation",
      determination: unknownCoworkProvenanceDetermination(),
      status: "awaiting_determination",
    });

    expect(stage.list(key)).toHaveLength(1);

    // Simulate a page/process restart: the volatile backing is gone, while
    // localStorage is still available to reconstruct the pending intent.
    const reopened = new DurableCoworkPasteProvenanceOutbox(
      key,
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
      stage,
    );
    const [recovered] = await reopened.list();
    expect(recovered).toMatchObject({
      idempotencyKey: "volatile-paste",
      status: "awaiting_determination",
    });
    expect(stage.list(key)).toHaveLength(1);

    await reopened.markReady(
      recovered!.id,
      humanDetermination(),
      "user_attestation",
    );
    expect(stage.list(key)[0]).toMatchObject({
      idempotencyKey: "volatile-paste",
      status: "ready",
      determination: {
        authorship: { kind: "human" },
      },
    });

    await reopened.remove(recovered!.id);
    expect(stage.list(key)).toEqual([]);
  });

  it("does not turn a non-retryable rejection into an endless retry", async () => {
    const outbox = new DurableCoworkPasteProvenanceOutbox(
      "store:terminal",
      new InMemoryCoworkPasteProvenanceOutboxBackingStore(),
      new InMemoryCoworkPasteProvenanceIntentStage(),
    );
    const entry = await outbox.append({
      anchor,
      idempotencyKey: "terminal-key",
      substantial: true,
      basisKind: "user_attestation",
      determination: unknownCoworkProvenanceDetermination(),
      status: "ready",
    });
    const failed = await outbox.markFailure(entry.id, {
      code: "invalid_attestation",
      message: "invalid",
      kind: "terminal",
    });
    expect(failed.status).toBe("terminal_failure");
  });

  it("quarantines malformed localStorage JSON without blocking the queue", () => {
    const storage = new MemoryStorage();
    const warnings: CoworkPasteProvenanceStorageWarning[] = [];
    const prefix = "test-paste-stage:";
    const key = "store:malformed-json";
    const raw = "{ definitely not valid JSON";
    storage.setItem(`${prefix}${key}`, raw);
    const stage = new WebStorageCoworkPasteProvenanceIntentStage(
      storage,
      prefix,
      (warning) => warnings.push(warning),
    );

    expect(stage.list(key)).toEqual([]);
    expect(storage.getItem(`${prefix}${key}`)).toBeNull();
    const quarantineKey = storage
      .keys()
      .find((candidate) =>
        candidate.startsWith(`${prefix}quarantine:${encodeURIComponent(key)}:`),
      );
    expect(quarantineKey).toBeDefined();
    expect(storage.getItem(quarantineKey!)).toBe(raw);
    expect(warnings).toEqual([
      expect.objectContaining({
        code: "malformed_intent_stage",
        key,
        quarantined: true,
        droppedEntries: 1,
      }),
    ]);
  });

  it("keeps valid staged intents while quarantining malformed and unbound identities", () => {
    const storage = new MemoryStorage();
    const warnings: CoworkPasteProvenanceStorageWarning[] = [];
    const prefix = "test-paste-stage:";
    const key = "store:partially-malformed";
    const valid = {
      anchor,
      idempotencyKey: "valid-paste",
      substantial: false,
      sourceKind: "paste",
      basisKind: "automatic_short_text_attribution",
      determination: humanDetermination(),
      capturedAt: "2026-07-30T12:00:00.000Z",
      passageExcerpt: anchor.exact,
      status: "ready",
    } as const;
    const legacyUnboundIdentity = {
      ...valid,
      idempotencyKey: "legacy-unbound-paste",
      determination: {
        ...valid.determination,
        authorship: {
          kind: "human",
          contributors: [{ kind: "current_user" }],
        },
      },
    };
    storage.setItem(
      `${prefix}${key}`,
      JSON.stringify([valid, legacyUnboundIdentity, { malformed: true }]),
    );
    const stage = new WebStorageCoworkPasteProvenanceIntentStage(
      storage,
      prefix,
      (warning) => warnings.push(warning),
    );

    expect(stage.list(key)).toEqual([valid]);
    expect(JSON.parse(storage.getItem(`${prefix}${key}`)!)).toEqual([valid]);
    expect(
      storage
        .keys()
        .some((candidate) =>
          candidate.startsWith(
            `${prefix}quarantine:${encodeURIComponent(key)}:`,
          ),
        ),
    ).toBe(true);
    expect(warnings).toEqual([
      expect.objectContaining({
        code: "malformed_intent_stage",
        key,
        quarantined: true,
        droppedEntries: 2,
      }),
    ]);
  });

  it("salvages valid IndexedDB entries and quarantines the malformed record", async () => {
    const key = "store:malformed-indexeddb";
    const validEntry = {
      id: 4,
      anchor,
      idempotencyKey: "valid-indexeddb-paste",
      substantial: false,
      sourceKind: "paste",
      basisKind: "automatic_short_text_attribution",
      determination: humanDetermination(),
      capturedAt: "2026-07-30T12:00:00.000Z",
      passageExcerpt: anchor.exact,
      status: "ready",
    } as const;
    const malformed = {
      key: "wrong-key",
      nextId: "not-a-number",
      entries: [validEntry, { malformed: true }],
    };
    const records = new Map<string, unknown>([[key, malformed]]);
    const warnings: CoworkPasteProvenanceStorageWarning[] = [];
    const backing = new IndexedDbCoworkPasteProvenanceOutboxBackingStore(
      "test-database",
      {
        openDatabase: async () => fakeIndexedDatabase(records),
        onWarning: (warning) => warnings.push(warning),
      },
    );

    const recovered = await backing.read(key);

    expect(recovered).toEqual({
      key,
      nextId: 5,
      entries: [validEntry],
    });
    expect(records.get(key)).toEqual(recovered);
    expect(
      [...records.keys()].some((candidate) =>
        candidate.startsWith(`__quarantine__:${encodeURIComponent(key)}:`),
      ),
    ).toBe(true);
    expect(warnings).toEqual([
      expect.objectContaining({
        code: "malformed_outbox_record",
        key,
        quarantined: true,
        droppedEntries: 1,
      }),
    ]);
  });

  it("retries IndexedDB initialization after a transient open failure", async () => {
    const records = new Map<string, unknown>();
    const database = fakeIndexedDatabase(records);
    const warnings: CoworkPasteProvenanceStorageWarning[] = [];
    let attempts = 0;
    const backing = new IndexedDbCoworkPasteProvenanceOutboxBackingStore(
      "test-retry-database",
      {
        openDatabase: async () => {
          attempts += 1;
          if (attempts === 1) {
            throw new Error("temporary browser failure");
          }
          return database;
        },
        onWarning: (warning) => warnings.push(warning),
      },
    );

    await expect(backing.read("store:retry")).rejects.toThrow(
      "temporary browser failure",
    );
    await expect(backing.read("store:retry")).resolves.toBeUndefined();
    expect(attempts).toBe(2);
    expect(warnings).toEqual([
      expect.objectContaining({
        code: "indexeddb_open_failed",
        quarantined: false,
      }),
    ]);
  });
});
