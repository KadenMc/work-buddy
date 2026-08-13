import type { RangeQuoteAnchor } from "../feedback/feedbackAnchor";
import {
  COWORK_PROVENANCE_DETERMINATION_SCHEMA,
  coworkProvenanceDeterminationIssue,
  type CoworkProvenanceActorIdentity,
  type CoworkProvenanceDetermination,
  type CoworkProvenancePerson,
} from "./contracts";
import {
  COWORK_PROVENANCE_ACTOR_CHANGED,
  coworkPastePassageExcerpt,
  coworkProvenanceExactWithinLimit,
  type CoworkPasteProvenanceRequest,
} from "./pasteProvenance";

const DATABASE_NAME = "work-buddy-cowork-paste-provenance";
const STORE_NAME = "paste-provenance-outbox";

export type CoworkPasteProvenanceStatus =
  | "capturing"
  | "cancelled"
  | "awaiting_determination"
  | "ready"
  | "retryable_failure"
  | "terminal_failure"
  | "stale_target";

export interface CoworkPasteProvenanceFailure {
  readonly code: string;
  readonly message: string;
  readonly kind: "retryable" | "terminal" | "stale_target";
}

export class CoworkPasteProvenanceExactLimitError extends Error {
  constructor() {
    super("The pasted passage exceeds the provenance API character limit.");
    this.name = "CoworkPasteProvenanceExactLimitError";
  }
}

/**
 * A local paste remains here until the provenance endpoint confirms its
 * receipt. `frozenRequest` is immutable across ambiguous transport failures.
 * It is cleared only after an explicit rejected-target recovery or when the
 * server proves that the acting identity changed and every pending
 * determination must be revisited.
 */
export interface CoworkPasteProvenanceOutboxEntry {
  readonly id: number;
  readonly anchor: RangeQuoteAnchor;
  readonly idempotencyKey: string;
  readonly substantial: boolean;
  /** Actor session in force when the input gesture began. */
  readonly capturedActor?: CoworkProvenanceActorIdentity;
  readonly sourceKind: CoworkPasteProvenanceRequest["sourceKind"];
  readonly basisKind: CoworkPasteProvenanceRequest["basisKind"];
  readonly determination: CoworkProvenanceDetermination;
  readonly capturedAt: string;
  readonly passageExcerpt: string;
  /** Server head known immediately before the local paste, for diagnosis only. */
  readonly capturedBaseStructuredHeadSha256?: string;
  /** Dismissal must retain this entry; only an explicit confirm may ready it. */
  readonly requiresExplicitDetermination?: boolean;
  readonly status: CoworkPasteProvenanceStatus;
  readonly frozenRequest?: CoworkPasteProvenanceRequest;
  readonly failure?: CoworkPasteProvenanceFailure;
}

export interface CoworkPasteProvenanceCapture {
  readonly anchor: RangeQuoteAnchor;
  readonly idempotencyKey: string;
  readonly substantial: boolean;
  readonly capturedActor?: CoworkProvenanceActorIdentity;
  /** Omitted only by persisted v1/v2 paste records and older adapters. */
  readonly sourceKind?: CoworkPasteProvenanceRequest["sourceKind"];
  readonly basisKind: CoworkPasteProvenanceRequest["basisKind"];
  readonly determination: CoworkProvenanceDetermination;
  readonly capturedAt?: string;
  readonly passageExcerpt?: string;
  readonly capturedBaseStructuredHeadSha256?: string;
  readonly requiresExplicitDetermination?: boolean;
  readonly status: Extract<
    CoworkPasteProvenanceStatus,
    "capturing" | "awaiting_determination" | "ready"
  >;
}

export interface CoworkPasteProvenanceOutbox {
  list(): Promise<readonly CoworkPasteProvenanceOutboxEntry[]>;
  append(
    capture: CoworkPasteProvenanceCapture,
  ): Promise<CoworkPasteProvenanceOutboxEntry>;
  /**
   * Synchronously stage, then durably insert/update one still-open typing
   * burst. The shared idempotency key is the burst identity.
   */
  upsertCapture(
    capture: CoworkPasteProvenanceCapture,
  ): Promise<CoworkPasteProvenanceOutboxEntry>;
  /** Retire an open burst whose entire inserted range was deleted. */
  cancelCapture(id: number): Promise<void>;
  /**
   * Reopen a direct-entry request frozen locally but not yet handed to the
   * recorder because another keystroke advanced its target generation.
   */
  reopenCapture(
    id: number,
    capture: CoworkPasteProvenanceCapture,
  ): Promise<CoworkPasteProvenanceOutboxEntry>;
  updateDetermination(
    id: number,
    determination: CoworkProvenanceDetermination,
  ): Promise<CoworkPasteProvenanceOutboxEntry>;
  markReady(
    id: number,
    determination: CoworkProvenanceDetermination,
    basisKind: CoworkPasteProvenanceRequest["basisKind"],
    capturedActor?: CoworkProvenanceActorIdentity,
  ): Promise<CoworkPasteProvenanceOutboxEntry>;
  freezeRequest(
    id: number,
    target: {
      readonly storeId: string;
      readonly documentId: string;
      readonly expectedStructuredHeadSha256: string;
    },
  ): Promise<CoworkPasteProvenanceOutboxEntry>;
  markFailure(
    id: number,
    failure: CoworkPasteProvenanceFailure,
  ): Promise<CoworkPasteProvenanceOutboxEntry>;
  /**
   * Atomically invalidate every pending determination after the server proves
   * that the acting identity changed. No entry may auto-send again until the
   * user explicitly confirms a fresh determination.
   */
  resetAfterActorChange(
    idempotencyKeyPrefix: string,
    determination: CoworkProvenanceDetermination,
  ): Promise<readonly CoworkPasteProvenanceOutboxEntry[]>;
  /**
   * Explicit rejected-target recovery starts a new server attempt. It is the
   * only operation allowed to replace the idempotency key or frozen target.
   */
  retarget(
    id: number,
    idempotencyKey: string,
    determination: CoworkProvenanceDetermination,
  ): Promise<CoworkPasteProvenanceOutboxEntry>;
  remove(id: number): Promise<void>;
}

interface PersistedOutbox {
  readonly key: string;
  readonly nextId: number;
  readonly entries: readonly CoworkPasteProvenanceOutboxEntry[];
}

export interface CoworkPasteProvenanceStorageWarning {
  readonly code:
    | "malformed_intent_stage"
    | "malformed_outbox_record"
    | "indexeddb_open_failed";
  readonly message: string;
  readonly key?: string;
  readonly quarantined: boolean;
  readonly droppedEntries?: number;
}

export type CoworkPasteProvenanceStorageWarningSink = (
  warning: CoworkPasteProvenanceStorageWarning,
) => void;

interface NormalizedValue<Value> {
  readonly value: Value;
  readonly repaired: boolean;
  readonly droppedEntries: number;
}

const storageWarning = (warning: CoworkPasteProvenanceStorageWarning): void => {
  console.warn(`[Co-work paste provenance] ${warning.message}`, warning);
};

let quarantineSequence = 0;
const uniqueSuffix = (): string => {
  quarantineSequence += 1;
  return `${Date.now().toString(36)}-${quarantineSequence.toString(36)}`;
};

const warningMessage = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const normalizedPerson = (value: unknown): CoworkProvenancePerson | null => {
  if (!isObject(value)) return null;
  if (
    value.kind === "current_user" &&
    typeof value.ref === "string" &&
    value.ref.trim().length > 0 &&
    (value.identity_status === "local_actor_ref" ||
      value.identity_status === "account_ref")
  ) {
    return {
      kind: "current_user",
      ref: value.ref,
      identity_status: value.identity_status,
    };
  }
  if (
    value.kind === "named_person" &&
    typeof value.display_name === "string" &&
    value.display_name.trim().length > 0
  ) {
    return { kind: "named_person", display_name: value.display_name };
  }
  return null;
};

const normalizedActor = (
  value: unknown,
): CoworkProvenanceActorIdentity | null => {
  if (
    !isObject(value) ||
    value.kind !== "human" ||
    typeof value.ref !== "string" ||
    value.ref.trim().length === 0 ||
    (value.identity_status !== "local_actor_ref" &&
      value.identity_status !== "account_ref")
  ) {
    return null;
  }
  return {
    kind: "human",
    ref: value.ref,
    identity_status: value.identity_status,
  };
};

const normalizedDetermination = (
  value: unknown,
): CoworkProvenanceDetermination | null => {
  if (
    !isObject(value) ||
    value.schema !== COWORK_PROVENANCE_DETERMINATION_SCHEMA ||
    !isObject(value.authorship) ||
    !Array.isArray(value.authorship.contributors) ||
    !isObject(value.human_review) ||
    !Array.isArray(value.human_review.reviewers)
  ) {
    return null;
  }
  const authorshipKind = value.authorship.kind;
  if (
    authorshipKind !== "human" &&
    authorshipKind !== "ai" &&
    authorshipKind !== "mixed" &&
    authorshipKind !== "unknown"
  ) {
    return null;
  }
  const contributors = value.authorship.contributors.map(normalizedPerson);
  const reviewers = value.human_review.reviewers.map(normalizedPerson);
  if (
    contributors.some((person) => person === null) ||
    reviewers.some((person) => person === null)
  ) {
    return null;
  }
  const reviewStatus = value.human_review.status;
  if (
    reviewStatus !== "reviewed" &&
    reviewStatus !== "not_reviewed" &&
    reviewStatus !== "not_applicable"
  ) {
    return null;
  }
  const determination = {
    schema: COWORK_PROVENANCE_DETERMINATION_SCHEMA,
    authorship: {
      kind: authorshipKind,
      contributors: contributors as CoworkProvenancePerson[],
    },
    human_review: {
      status: reviewStatus,
      reviewers: reviewers as CoworkProvenancePerson[],
    },
  } as CoworkProvenanceDetermination;
  try {
    return coworkProvenanceDeterminationIssue(determination) === null
      ? determination
      : null;
  } catch {
    return null;
  }
};

const normalizedAnchor = (value: unknown): RangeQuoteAnchor | null => {
  if (
    !isObject(value) ||
    typeof value.exact !== "string" ||
    value.exact.length === 0 ||
    !coworkProvenanceExactWithinLimit(value.exact) ||
    typeof value.prefix !== "string" ||
    typeof value.suffix !== "string"
  ) {
    return null;
  }
  return {
    exact: value.exact,
    prefix: value.prefix,
    suffix: value.suffix,
  };
};

const PROVENANCE_BASIS_KINDS = new Set([
  "automatic_short_text_attribution",
  "automatic_direct_entry_attribution",
  "user_attestation",
]);
const PROVENANCE_SOURCE_KINDS = new Set(["paste", "direct_entry", "legacy"]);
const provenanceSourceBasisAllowed = (
  sourceKind: CoworkPasteProvenanceRequest["sourceKind"],
  basisKind: CoworkPasteProvenanceRequest["basisKind"],
): boolean =>
  (sourceKind === "paste" &&
    (basisKind === "automatic_short_text_attribution" ||
      basisKind === "user_attestation")) ||
  (sourceKind === "direct_entry" &&
    basisKind === "automatic_direct_entry_attribution") ||
  (sourceKind === "legacy" && basisKind === "user_attestation");
const PASTE_STATUSES = new Set<CoworkPasteProvenanceStatus>([
  "capturing",
  "cancelled",
  "awaiting_determination",
  "ready",
  "retryable_failure",
  "terminal_failure",
  "stale_target",
]);

const normalizedCapture = (
  value: unknown,
  { persisted }: { readonly persisted: boolean },
): CoworkPasteProvenanceCapture | CoworkPasteProvenanceOutboxEntry | null => {
  if (!isObject(value)) return null;
  const anchor = normalizedAnchor(value.anchor);
  const determination = normalizedDetermination(value.determination);
  if (
    anchor === null ||
    determination === null ||
    typeof value.idempotencyKey !== "string" ||
    value.idempotencyKey.length === 0 ||
    typeof value.substantial !== "boolean" ||
    (value.sourceKind !== undefined &&
      (typeof value.sourceKind !== "string" ||
        !PROVENANCE_SOURCE_KINDS.has(value.sourceKind))) ||
    typeof value.basisKind !== "string" ||
    !PROVENANCE_BASIS_KINDS.has(value.basisKind) ||
    typeof value.status !== "string" ||
    !PASTE_STATUSES.has(value.status as CoworkPasteProvenanceStatus)
  ) {
    return null;
  }
  const capturedActor =
    value.capturedActor === undefined
      ? undefined
      : normalizedActor(value.capturedActor);
  if (value.capturedActor !== undefined && capturedActor === null) return null;
  const sourceKind = (value.sourceKind ??
    "paste") as CoworkPasteProvenanceRequest["sourceKind"];
  const basisKind =
    value.basisKind as CoworkPasteProvenanceRequest["basisKind"];
  if (
    !provenanceSourceBasisAllowed(sourceKind, basisKind) ||
    (value.status === "capturing" &&
      (sourceKind !== "direct_entry" ||
        basisKind !== "automatic_direct_entry_attribution")) ||
    (!persisted && value.status === "cancelled")
  ) {
    return null;
  }
  if (
    !persisted &&
    value.status !== "capturing" &&
    value.status !== "awaiting_determination" &&
    value.status !== "ready"
  ) {
    return null;
  }
  if (value.capturedAt !== undefined && typeof value.capturedAt !== "string") {
    return null;
  }
  if (
    value.passageExcerpt !== undefined &&
    typeof value.passageExcerpt !== "string"
  ) {
    return null;
  }
  if (
    value.capturedBaseStructuredHeadSha256 !== undefined &&
    typeof value.capturedBaseStructuredHeadSha256 !== "string"
  ) {
    return null;
  }
  if (
    value.requiresExplicitDetermination !== undefined &&
    typeof value.requiresExplicitDetermination !== "boolean"
  ) {
    return null;
  }

  const base = {
    anchor,
    idempotencyKey: value.idempotencyKey,
    substantial: value.substantial,
    // v1/v2 records predate the explicit source discriminator and were all
    // paste captures. Preserve them without inventing a new source.
    sourceKind,
    basisKind,
    determination,
    ...(capturedActor == null ? {} : { capturedActor }),
    ...(typeof value.capturedAt === "string"
      ? { capturedAt: value.capturedAt }
      : persisted
        ? { capturedAt: new Date(0).toISOString() }
        : {}),
    ...(typeof value.passageExcerpt === "string"
      ? { passageExcerpt: value.passageExcerpt }
      : persisted
        ? { passageExcerpt: coworkPastePassageExcerpt(anchor.exact) }
        : {}),
    ...(typeof value.capturedBaseStructuredHeadSha256 === "string"
      ? {
          capturedBaseStructuredHeadSha256:
            value.capturedBaseStructuredHeadSha256,
        }
      : {}),
    ...(value.requiresExplicitDetermination === true
      ? { requiresExplicitDetermination: true }
      : {}),
    status: value.status as CoworkPasteProvenanceStatus,
  };
  if (!persisted) {
    return base as CoworkPasteProvenanceCapture;
  }
  if (
    typeof value.id !== "number" ||
    !Number.isSafeInteger(value.id) ||
    value.id <= 0
  ) {
    return null;
  }

  let frozenRequest: CoworkPasteProvenanceRequest | undefined;
  if (value.frozenRequest !== undefined) {
    if (!isObject(value.frozenRequest)) return null;
    const frozenAnchor = normalizedAnchor(value.frozenRequest.anchor);
    const frozenAttestation = normalizedDetermination(
      value.frozenRequest.attestation,
    );
    if (
      frozenAnchor === null ||
      frozenAttestation === null ||
      typeof value.frozenRequest.storeId !== "string" ||
      value.frozenRequest.storeId.length === 0 ||
      typeof value.frozenRequest.documentId !== "string" ||
      value.frozenRequest.documentId.length === 0 ||
      typeof value.frozenRequest.expectedStructuredHeadSha256 !== "string" ||
      typeof value.frozenRequest.idempotencyKey !== "string" ||
      value.frozenRequest.idempotencyKey !== value.idempotencyKey ||
      typeof value.frozenRequest.basisKind !== "string" ||
      !PROVENANCE_BASIS_KINDS.has(value.frozenRequest.basisKind) ||
      (value.frozenRequest.sourceKind !== undefined &&
        (typeof value.frozenRequest.sourceKind !== "string" ||
          !PROVENANCE_SOURCE_KINDS.has(value.frozenRequest.sourceKind))) ||
      (value.frozenRequest.expectedActorRef !== undefined &&
        typeof value.frozenRequest.expectedActorRef !== "string") ||
      (value.frozenRequest.expectedActorIdentityStatus !== undefined &&
        value.frozenRequest.expectedActorIdentityStatus !== "local_actor_ref" &&
        value.frozenRequest.expectedActorIdentityStatus !== "account_ref")
    ) {
      return null;
    }
    frozenRequest = {
      storeId: value.frozenRequest.storeId,
      documentId: value.frozenRequest.documentId,
      ...(typeof value.frozenRequest.expectedActorRef === "string"
        ? { expectedActorRef: value.frozenRequest.expectedActorRef }
        : {}),
      ...(value.frozenRequest.expectedActorIdentityStatus ===
        "local_actor_ref" ||
      value.frozenRequest.expectedActorIdentityStatus === "account_ref"
        ? {
            expectedActorIdentityStatus:
              value.frozenRequest.expectedActorIdentityStatus,
          }
        : {}),
      sourceKind: (value.frozenRequest.sourceKind ??
        "paste") as CoworkPasteProvenanceRequest["sourceKind"],
      basisKind: value.frozenRequest
        .basisKind as CoworkPasteProvenanceRequest["basisKind"],
      expectedStructuredHeadSha256:
        value.frozenRequest.expectedStructuredHeadSha256,
      anchor: frozenAnchor,
      attestation: frozenAttestation,
      idempotencyKey: value.frozenRequest.idempotencyKey,
    };
    if (
      value.status === "capturing" ||
      value.status === "cancelled" ||
      frozenRequest.sourceKind !== sourceKind ||
      frozenRequest.basisKind !== basisKind ||
      !portableEqual(frozenRequest.anchor, anchor) ||
      !portableEqual(frozenRequest.attestation, determination) ||
      (frozenRequest.sourceKind !== "paste" &&
        (frozenRequest.expectedActorRef === undefined ||
          frozenRequest.expectedActorIdentityStatus === undefined)) ||
      (capturedActor != null &&
        (frozenRequest.expectedActorRef !== capturedActor.ref ||
          frozenRequest.expectedActorIdentityStatus !==
            capturedActor.identity_status))
    ) {
      return null;
    }
  }

  let failure: CoworkPasteProvenanceFailure | undefined;
  if (value.failure !== undefined) {
    if (
      !isObject(value.failure) ||
      typeof value.failure.code !== "string" ||
      typeof value.failure.message !== "string" ||
      (value.failure.kind !== "retryable" &&
        value.failure.kind !== "terminal" &&
        value.failure.kind !== "stale_target")
    ) {
      return null;
    }
    failure = {
      code: value.failure.code,
      message: value.failure.message,
      kind: value.failure.kind,
    };
  }
  return {
    ...base,
    id: value.id,
    capturedAt: base.capturedAt!,
    passageExcerpt: base.passageExcerpt!,
    ...(frozenRequest === undefined ? {} : { frozenRequest }),
    ...(failure === undefined ? {} : { failure }),
  } as CoworkPasteProvenanceOutboxEntry;
};

const portableEqual = (
  left: unknown,
  right: unknown,
  seen = new WeakMap<object, WeakSet<object>>(),
): boolean => {
  if (Object.is(left, right)) return true;
  if (
    typeof left !== "object" ||
    left === null ||
    typeof right !== "object" ||
    right === null
  ) {
    return false;
  }
  const rightSeen = seen.get(left);
  if (rightSeen?.has(right)) return true;
  if (rightSeen === undefined) {
    seen.set(left, new WeakSet([right]));
  } else {
    rightSeen.add(right);
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => portableEqual(value, right[index], seen))
    );
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord)
    .filter((key) => leftRecord[key] !== undefined)
    .sort();
  const rightKeys = Object.keys(rightRecord)
    .filter((key) => rightRecord[key] !== undefined)
    .sort();
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key, index) =>
        key === rightKeys[index] &&
        portableEqual(leftRecord[key], rightRecord[key], seen),
    )
  );
};

const normalizeIntentCaptures = (
  value: unknown,
): NormalizedValue<readonly CoworkPasteProvenanceCapture[]> => {
  if (!Array.isArray(value)) {
    return { value: [], repaired: true, droppedEntries: 1 };
  }
  const captures: CoworkPasteProvenanceCapture[] = [];
  const seen = new Set<string>();
  let droppedEntries = 0;
  for (const candidate of value) {
    const normalized = normalizedCapture(candidate, { persisted: false });
    if (normalized === null || seen.has(normalized.idempotencyKey)) {
      droppedEntries += 1;
      continue;
    }
    seen.add(normalized.idempotencyKey);
    captures.push(normalized as CoworkPasteProvenanceCapture);
  }
  return {
    value: captures,
    repaired: droppedEntries > 0 || !portableEqual(value, captures),
    droppedEntries,
  };
};

const normalizePersistedOutbox = (
  value: unknown,
  key: string,
): NormalizedValue<PersistedOutbox> => {
  const rawEntries =
    isObject(value) && Array.isArray(value.entries) ? value.entries : [];
  const entries: CoworkPasteProvenanceOutboxEntry[] = [];
  const seenIds = new Set<number>();
  const seenKeys = new Set<string>();
  let droppedEntries =
    isObject(value) && Array.isArray(value.entries)
      ? 0
      : value === undefined
        ? 0
        : 1;
  for (const candidate of rawEntries) {
    const normalized = normalizedCapture(candidate, { persisted: true });
    if (
      normalized === null ||
      seenIds.has((normalized as CoworkPasteProvenanceOutboxEntry).id) ||
      seenKeys.has(normalized.idempotencyKey)
    ) {
      droppedEntries += 1;
      continue;
    }
    const entry = normalized as CoworkPasteProvenanceOutboxEntry;
    seenIds.add(entry.id);
    seenKeys.add(entry.idempotencyKey);
    entries.push(entry);
  }
  const minimumNextId = Math.max(0, ...entries.map((entry) => entry.id)) + 1;
  const requestedNextId =
    isObject(value) &&
    typeof value.nextId === "number" &&
    Number.isSafeInteger(value.nextId) &&
    value.nextId >= minimumNextId
      ? value.nextId
      : minimumNextId;
  const record: PersistedOutbox = {
    key,
    nextId: requestedNextId,
    entries,
  };
  return {
    value: record,
    repaired:
      value !== undefined &&
      (droppedEntries > 0 || !portableEqual(value, record)),
    droppedEntries,
  };
};

export interface CoworkPasteProvenanceOutboxBackingStore {
  /**
   * True only when a successful mutation survives a page/process restart.
   * Volatile fallbacks must keep the synchronous recovery journal staged.
   */
  readonly durable: boolean;
  read(key: string): Promise<PersistedOutbox | undefined>;
  mutate<Value>(
    key: string,
    mutation: (current: PersistedOutbox) => {
      readonly record: PersistedOutbox;
      readonly result: Value;
    },
  ): Promise<Value>;
}

export interface CoworkPasteProvenanceIntentStage {
  list(key: string): readonly CoworkPasteProvenanceCapture[];
  put(key: string, capture: CoworkPasteProvenanceCapture): void;
  remove(key: string, idempotencyKey: string): void;
}

const cloneEntry = (
  entry: CoworkPasteProvenanceOutboxEntry,
): CoworkPasteProvenanceOutboxEntry => {
  const normalized = normalizedCapture(entry, { persisted: true });
  if (normalized === null) {
    throw new Error("Co-work paste provenance outbox entry is malformed");
  }
  return normalized as CoworkPasteProvenanceOutboxEntry;
};

const cloneRecord = (record: PersistedOutbox): PersistedOutbox => {
  const normalized = normalizePersistedOutbox(record, record.key);
  if (normalized.repaired) {
    throw new Error("Co-work paste provenance outbox record is malformed");
  }
  return normalized.value;
};

const emptyRecord = (key: string): PersistedOutbox => ({
  key,
  nextId: 1,
  entries: [],
});

export class InMemoryCoworkPasteProvenanceOutboxBackingStore implements CoworkPasteProvenanceOutboxBackingStore {
  readonly durable = false;
  readonly #records = new Map<string, PersistedOutbox>();
  #chain: Promise<unknown> = Promise.resolve();

  async read(key: string): Promise<PersistedOutbox | undefined> {
    await this.#chain;
    const value = this.#records.get(key);
    return value === undefined ? undefined : cloneRecord(value);
  }

  mutate<Value>(
    key: string,
    mutation: (current: PersistedOutbox) => {
      readonly record: PersistedOutbox;
      readonly result: Value;
    },
  ): Promise<Value> {
    const run = this.#chain.then(() => {
      const current = cloneRecord(this.#records.get(key) ?? emptyRecord(key));
      const { record, result } = mutation(current);
      if (record.key !== key) {
        throw new Error(
          "Co-work paste provenance outbox key does not match its record",
        );
      }
      this.#records.set(key, cloneRecord(record));
      return result;
    });
    this.#chain = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }
}

export class InMemoryCoworkPasteProvenanceIntentStage implements CoworkPasteProvenanceIntentStage {
  readonly #records = new Map<
    string,
    readonly CoworkPasteProvenanceCapture[]
  >();

  list(key: string): readonly CoworkPasteProvenanceCapture[] {
    return normalizeIntentCaptures(this.#records.get(key) ?? []).value;
  }

  put(key: string, capture: CoworkPasteProvenanceCapture): void {
    const current = this.list(key);
    const found = current.some(
      (candidate) => candidate.idempotencyKey === capture.idempotencyKey,
    );
    this.#records.set(
      key,
      found
        ? current.map((candidate) =>
            candidate.idempotencyKey === capture.idempotencyKey
              ? capture
              : candidate,
          )
        : [...current, capture],
    );
  }

  remove(key: string, idempotencyKey: string): void {
    this.#records.set(
      key,
      this.list(key).filter(
        (capture) => capture.idempotencyKey !== idempotencyKey,
      ),
    );
  }
}

export class WebStorageCoworkPasteProvenanceIntentStage implements CoworkPasteProvenanceIntentStage {
  readonly #storage: Storage;
  readonly #prefix: string;
  readonly #onWarning: CoworkPasteProvenanceStorageWarningSink;

  constructor(
    storage: Storage = globalThis.localStorage,
    prefix = "work-buddy-cowork-paste-intent:",
    onWarning: CoworkPasteProvenanceStorageWarningSink = storageWarning,
  ) {
    this.#storage = storage;
    this.#prefix = prefix;
    this.#onWarning = onWarning;
  }

  list(key: string): readonly CoworkPasteProvenanceCapture[] {
    const storageKey = `${this.#prefix}${key}`;
    const value = this.#storage.getItem(storageKey);
    if (value === null) return [];
    let parsed: unknown;
    try {
      parsed = JSON.parse(value) as unknown;
    } catch {
      const quarantined = this.#quarantine(key, value);
      const warning: CoworkPasteProvenanceStorageWarning = {
        code: "malformed_intent_stage",
        message:
          "A malformed staged paste-provenance record was removed so pending work can continue.",
        key,
        quarantined,
        droppedEntries: 1,
      };
      try {
        this.#storage.removeItem(storageKey);
      } catch (error) {
        this.#warn({
          ...warning,
          message: `${warning.message} Recovery failed: ${warningMessage(error)}`,
        });
        throw error;
      }
      this.#warn(warning);
      return [];
    }
    const normalized = normalizeIntentCaptures(parsed);
    if (normalized.repaired) {
      const quarantined = this.#quarantine(key, value);
      const warning: CoworkPasteProvenanceStorageWarning = {
        code: "malformed_intent_stage",
        message:
          "Malformed staged paste-provenance entries were isolated; valid entries remain pending.",
        key,
        quarantined,
        droppedEntries: normalized.droppedEntries,
      };
      try {
        this.#write(key, normalized.value);
      } catch (error) {
        this.#warn({
          ...warning,
          message: `${warning.message} Recovery failed: ${warningMessage(error)}`,
        });
        throw error;
      }
      this.#warn(warning);
    }
    return normalized.value;
  }

  put(key: string, capture: CoworkPasteProvenanceCapture): void {
    const current = this.list(key);
    const found = current.some(
      (candidate) => candidate.idempotencyKey === capture.idempotencyKey,
    );
    this.#write(
      key,
      found
        ? current.map((candidate) =>
            candidate.idempotencyKey === capture.idempotencyKey
              ? capture
              : candidate,
          )
        : [...current, capture],
    );
  }

  remove(key: string, idempotencyKey: string): void {
    this.#write(
      key,
      this.list(key).filter(
        (capture) => capture.idempotencyKey !== idempotencyKey,
      ),
    );
  }

  #write(key: string, captures: readonly CoworkPasteProvenanceCapture[]): void {
    const storageKey = `${this.#prefix}${key}`;
    if (captures.length === 0) {
      this.#storage.removeItem(storageKey);
    } else {
      this.#storage.setItem(storageKey, JSON.stringify(captures));
    }
  }

  #quarantine(key: string, raw: string): boolean {
    try {
      this.#storage.setItem(
        `${this.#prefix}quarantine:${encodeURIComponent(key)}:${uniqueSuffix()}`,
        raw,
      );
      return true;
    } catch {
      return false;
    }
  }

  #warn(warning: CoworkPasteProvenanceStorageWarning): void {
    try {
      this.#onWarning(warning);
    } catch {
      // Diagnostic reporting must never block recovery of pending work.
    }
  }
}

const requestResult = <Value>(request: IDBRequest<Value>): Promise<Value> =>
  new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(
        request.error ??
          new Error("IndexedDB paste provenance outbox request failed"),
      );
  });

const transactionDone = (transaction: IDBTransaction): Promise<void> =>
  new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () =>
      reject(
        transaction.error ??
          new Error("IndexedDB paste provenance transaction failed"),
      );
    transaction.onabort = () =>
      reject(
        transaction.error ??
          new Error("IndexedDB paste provenance transaction aborted"),
      );
  });

const openIndexedDb = (databaseName: string): Promise<IDBDatabase> =>
  new Promise((resolve, reject) => {
    if (globalThis.indexedDB === undefined) {
      reject(new Error("IndexedDB is not available"));
      return;
    }
    const request = globalThis.indexedDB.open(databaseName, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME)) {
        request.result.createObjectStore(STORE_NAME, { keyPath: "key" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () =>
      reject(
        request.error ??
          new Error("Could not open Co-work paste provenance outbox"),
      );
  });

interface QuarantinedOutboxRecord {
  readonly key: string;
  readonly schema: "cowork-paste-provenance-quarantine/v1";
  readonly originalKey: string;
  readonly quarantinedAt: string;
  readonly value: unknown;
}

const quarantinedOutboxRecord = (
  key: string,
  value: unknown,
): QuarantinedOutboxRecord => ({
  key: `__quarantine__:${encodeURIComponent(key)}:${uniqueSuffix()}`,
  schema: "cowork-paste-provenance-quarantine/v1",
  originalKey: key,
  quarantinedAt: new Date().toISOString(),
  value,
});

export interface IndexedDbCoworkPasteProvenanceOutboxBackingStoreOptions {
  readonly openDatabase?: (databaseName: string) => Promise<IDBDatabase>;
  readonly onWarning?: CoworkPasteProvenanceStorageWarningSink;
}

export class IndexedDbCoworkPasteProvenanceOutboxBackingStore implements CoworkPasteProvenanceOutboxBackingStore {
  readonly durable = true;
  readonly #databaseName: string;
  readonly #openDatabase: (databaseName: string) => Promise<IDBDatabase>;
  readonly #onWarning: CoworkPasteProvenanceStorageWarningSink;
  #database?: Promise<IDBDatabase>;

  constructor(
    databaseName = DATABASE_NAME,
    options: IndexedDbCoworkPasteProvenanceOutboxBackingStoreOptions = {},
  ) {
    this.#databaseName = databaseName;
    this.#openDatabase = options.openDatabase ?? openIndexedDb;
    this.#onWarning = options.onWarning ?? storageWarning;
  }

  async read(key: string): Promise<PersistedOutbox | undefined> {
    const database = await this.#open();
    const transaction = database.transaction(STORE_NAME, "readwrite");
    const done = transactionDone(transaction);
    const store = transaction.objectStore(STORE_NAME);
    let warning: CoworkPasteProvenanceStorageWarning | undefined;
    try {
      const stored = (await requestResult(store.get(key))) as unknown;
      if (stored === undefined) {
        await done;
        return undefined;
      }
      const normalized = normalizePersistedOutbox(stored, key);
      if (normalized.repaired) {
        store.put(quarantinedOutboxRecord(key, stored));
        store.put(normalized.value);
        warning = {
          code: "malformed_outbox_record",
          message:
            "A malformed paste-provenance outbox record was isolated; valid entries remain pending.",
          key,
          quarantined: true,
          droppedEntries: normalized.droppedEntries,
        };
      }
      await done;
      if (warning !== undefined) this.#warn(warning);
      return cloneRecord(normalized.value);
    } catch (error) {
      try {
        transaction.abort();
      } catch {
        // The transaction may already have completed after a request error.
      }
      await done.catch(() => undefined);
      if (warning !== undefined) {
        this.#warn({
          ...warning,
          message: `${warning.message} Recovery failed: ${warningMessage(error)}`,
          quarantined: false,
        });
      }
      throw error;
    }
  }

  async mutate<Value>(
    key: string,
    mutation: (current: PersistedOutbox) => {
      readonly record: PersistedOutbox;
      readonly result: Value;
    },
  ): Promise<Value> {
    const database = await this.#open();
    const transaction = database.transaction(STORE_NAME, "readwrite");
    const done = transactionDone(transaction);
    const store = transaction.objectStore(STORE_NAME);
    let warning: CoworkPasteProvenanceStorageWarning | undefined;
    try {
      const stored = (await requestResult(store.get(key))) as unknown;
      const normalized = normalizePersistedOutbox(stored, key);
      if (normalized.repaired) {
        store.put(quarantinedOutboxRecord(key, stored));
        warning = {
          code: "malformed_outbox_record",
          message:
            "A malformed paste-provenance outbox record was isolated before pending work was updated.",
          key,
          quarantined: true,
          droppedEntries: normalized.droppedEntries,
        };
      }
      const { record, result } = mutation(cloneRecord(normalized.value));
      if (record.key !== key) {
        transaction.abort();
        await done.catch(() => undefined);
        throw new Error(
          "Co-work paste provenance outbox key does not match its record",
        );
      }
      const validated = normalizePersistedOutbox(record, key);
      if (validated.repaired) {
        transaction.abort();
        await done.catch(() => undefined);
        throw new Error(
          "Co-work paste provenance mutation produced a malformed record",
        );
      }
      store.put(validated.value);
      await done;
      if (warning !== undefined) this.#warn(warning);
      return result;
    } catch (error) {
      try {
        transaction.abort();
      } catch {
        // The transaction may already have completed after a request error.
      }
      await done.catch(() => undefined);
      if (warning !== undefined) {
        this.#warn({
          ...warning,
          message: `${warning.message} Recovery failed: ${warningMessage(error)}`,
          quarantined: false,
        });
      }
      throw error;
    }
  }

  #open(): Promise<IDBDatabase> {
    if (this.#database !== undefined) return this.#database;
    const opening = this.#openDatabase(this.#databaseName);
    this.#database = opening;
    void opening.catch((error: unknown) => {
      if (this.#database === opening) {
        this.#database = undefined;
      }
      this.#warn({
        code: "indexeddb_open_failed",
        message: `The paste-provenance outbox could not open and will retry: ${warningMessage(error)}`,
        quarantined: false,
      });
    });
    return opening;
  }

  #warn(warning: CoworkPasteProvenanceStorageWarning): void {
    try {
      this.#onWarning(warning);
    } catch {
      // Diagnostic reporting must never block recovery of pending work.
    }
  }
}

const fallbackBacking = new InMemoryCoworkPasteProvenanceOutboxBackingStore();
const fallbackIntentStage = new InMemoryCoworkPasteProvenanceIntentStage();

export class DurableCoworkPasteProvenanceOutbox implements CoworkPasteProvenanceOutbox {
  readonly #key: string;
  readonly #backing: CoworkPasteProvenanceOutboxBackingStore;
  readonly #intentStage: CoworkPasteProvenanceIntentStage;
  #chain: Promise<unknown> = Promise.resolve();

  constructor(
    key: string,
    backing: CoworkPasteProvenanceOutboxBackingStore = typeof indexedDB ===
    "undefined"
      ? fallbackBacking
      : new IndexedDbCoworkPasteProvenanceOutboxBackingStore(),
    intentStage: CoworkPasteProvenanceIntentStage = typeof localStorage ===
    "undefined"
      ? fallbackIntentStage
      : new WebStorageCoworkPasteProvenanceIntentStage(),
  ) {
    this.#key = key;
    this.#backing = backing;
    this.#intentStage = intentStage;
  }

  list(): Promise<readonly CoworkPasteProvenanceOutboxEntry[]> {
    return this.#enqueue(async () => {
      await this.#reconcileStagedIntents();
      return this.#backing.mutate(this.#key, (current) => {
        const entries = current.entries.filter(
          (entry) => entry.status !== "cancelled",
        );
        return {
          record: { ...current, entries },
          result: entries.map(cloneEntry),
        };
      });
    });
  }

  append(
    capture: CoworkPasteProvenanceCapture,
  ): Promise<CoworkPasteProvenanceOutboxEntry> {
    if (!coworkProvenanceExactWithinLimit(capture.anchor.exact)) {
      return Promise.reject(new CoworkPasteProvenanceExactLimitError());
    }
    const normalized: CoworkPasteProvenanceCapture = {
      ...capture,
      sourceKind: capture.sourceKind ?? "paste",
      capturedAt: capture.capturedAt ?? new Date().toISOString(),
      passageExcerpt:
        capture.passageExcerpt ??
        coworkPastePassageExcerpt(capture.anchor.exact),
    };
    if (
      normalized.status === "capturing" ||
      !provenanceSourceBasisAllowed(
        normalized.sourceKind ?? "paste",
        normalized.basisKind,
      )
    ) {
      return Promise.reject(
        new Error(
          "This provenance capture has an invalid source, basis, or state.",
        ),
      );
    }
    try {
      // This synchronous journal is the smallest honest cross-store barrier:
      // Yjs and IndexedDB cannot participate in one atomic browser transaction.
      this.#intentStage.put(this.#key, normalized);
    } catch (error) {
      return Promise.reject(error);
    }
    return this.#update((current) => {
      const existing = current.entries.find(
        (entry) => entry.idempotencyKey === normalized.idempotencyKey,
      );
      if (existing !== undefined) {
        return { record: current, result: cloneEntry(existing) };
      }
      const entry: CoworkPasteProvenanceOutboxEntry = {
        id: current.nextId,
        ...normalized,
        sourceKind: normalized.sourceKind ?? "paste",
        capturedAt: normalized.capturedAt!,
        passageExcerpt: normalized.passageExcerpt!,
      };
      return {
        record: {
          ...current,
          nextId: current.nextId + 1,
          entries: [...current.entries, entry],
        },
        result: cloneEntry(entry),
      };
    }).then((entry) => {
      if (this.#backing.durable) {
        try {
          this.#intentStage.remove(this.#key, normalized.idempotencyKey);
        } catch {
          // A leftover staged intent is safe: reconciliation deduplicates by key.
        }
      }
      return entry;
    });
  }

  upsertCapture(
    capture: CoworkPasteProvenanceCapture,
  ): Promise<CoworkPasteProvenanceOutboxEntry> {
    if (!coworkProvenanceExactWithinLimit(capture.anchor.exact)) {
      return Promise.reject(new CoworkPasteProvenanceExactLimitError());
    }
    if (
      capture.status !== "capturing" ||
      capture.sourceKind !== "direct_entry" ||
      capture.basisKind !== "automatic_direct_entry_attribution"
    ) {
      return Promise.reject(
        new Error("Only an automatic direct-entry burst can be kept open."),
      );
    }
    const normalized: CoworkPasteProvenanceCapture = {
      ...capture,
      sourceKind: "direct_entry",
      capturedAt: capture.capturedAt ?? new Date().toISOString(),
      passageExcerpt:
        capture.passageExcerpt ??
        coworkPastePassageExcerpt(capture.anchor.exact),
    };
    try {
      // This overwrite is synchronous. A crash can lose neither the first
      // character nor the latest shape of a still-open typing burst merely
      // because IndexedDB has not run yet.
      this.#intentStage.put(this.#key, normalized);
    } catch (error) {
      return Promise.reject(error);
    }
    return this.#update((current) => {
      const existing = current.entries.find(
        (entry) => entry.idempotencyKey === normalized.idempotencyKey,
      );
      if (existing !== undefined) {
        if (
          existing.status !== "capturing" ||
          existing.frozenRequest !== undefined
        ) {
          throw new Error("A frozen provenance capture cannot be extended.");
        }
        const next: CoworkPasteProvenanceOutboxEntry = {
          id: existing.id,
          ...normalized,
          sourceKind: normalized.sourceKind ?? "paste",
          capturedAt: normalized.capturedAt!,
          passageExcerpt: normalized.passageExcerpt!,
        };
        return {
          record: {
            ...current,
            entries: current.entries.map((entry) =>
              entry.id === existing.id ? next : entry,
            ),
          },
          result: cloneEntry(next),
        };
      }
      const entry: CoworkPasteProvenanceOutboxEntry = {
        id: current.nextId,
        ...normalized,
        sourceKind: normalized.sourceKind ?? "paste",
        capturedAt: normalized.capturedAt!,
        passageExcerpt: normalized.passageExcerpt!,
      };
      return {
        record: {
          ...current,
          nextId: current.nextId + 1,
          entries: [...current.entries, entry],
        },
        result: cloneEntry(entry),
      };
    });
  }

  cancelCapture(id: number): Promise<void> {
    return this.#replace(id, (entry) => {
      if (entry.status !== "capturing" || entry.frozenRequest !== undefined) {
        throw new Error("Only an open provenance capture can be cancelled.");
      }
      return { ...entry, status: "cancelled" };
    }).then((entry) => {
      // The persisted tombstone wins if the page stops before physical cleanup.
      this.#intentStage.remove(this.#key, entry.idempotencyKey);
      return this.#update((current) => ({
        record: {
          ...current,
          entries: current.entries.filter((candidate) => candidate.id !== id),
        },
        result: undefined,
      }));
    });
  }

  reopenCapture(
    id: number,
    capture: CoworkPasteProvenanceCapture,
  ): Promise<CoworkPasteProvenanceOutboxEntry> {
    if (
      capture.status !== "capturing" ||
      capture.sourceKind !== "direct_entry" ||
      capture.basisKind !== "automatic_direct_entry_attribution"
    ) {
      return Promise.reject(
        new Error("Only an automatic direct-entry request can be reopened."),
      );
    }
    try {
      this.#intentStage.put(this.#key, capture);
    } catch (error) {
      return Promise.reject(error);
    }
    return this.#replace(id, (entry) => {
      if (
        entry.sourceKind !== "direct_entry" ||
        entry.idempotencyKey !== capture.idempotencyKey
      ) {
        throw new Error(
          "Only the same unsent direct-entry request can be reopened.",
        );
      }
      return {
        id: entry.id,
        ...capture,
        sourceKind: "direct_entry",
        capturedAt: capture.capturedAt ?? entry.capturedAt,
        passageExcerpt:
          capture.passageExcerpt ??
          coworkPastePassageExcerpt(capture.anchor.exact),
      };
    });
  }

  updateDetermination(
    id: number,
    determination: CoworkProvenanceDetermination,
  ): Promise<CoworkPasteProvenanceOutboxEntry> {
    return this.#replace(id, (entry) => {
      if (entry.frozenRequest !== undefined) {
        throw new Error("A frozen provenance request cannot be edited.");
      }
      return {
        ...entry,
        determination,
        failure:
          entry.failure?.code === COWORK_PROVENANCE_ACTOR_CHANGED
            ? entry.failure
            : undefined,
      };
    }).then((entry) =>
      this.#refreshVolatileIntent(entry, entry.idempotencyKey),
    );
  }

  markReady(
    id: number,
    determination: CoworkProvenanceDetermination,
    basisKind: CoworkPasteProvenanceRequest["basisKind"],
    capturedActor?: CoworkProvenanceActorIdentity,
  ): Promise<CoworkPasteProvenanceOutboxEntry> {
    return this.#replace(id, (entry) => {
      if (entry.frozenRequest !== undefined) {
        throw new Error("A frozen provenance request is already ready.");
      }
      if (!provenanceSourceBasisAllowed(entry.sourceKind, basisKind)) {
        throw new Error("The provenance source and basis are incompatible.");
      }
      return {
        ...entry,
        determination,
        basisKind,
        ...(capturedActor === undefined ? {} : { capturedActor }),
        status: "ready",
        requiresExplicitDetermination: undefined,
        failure: undefined,
      };
    }).then((entry) => {
      if (this.#backing.durable) {
        this.#intentStage.remove(this.#key, entry.idempotencyKey);
        return entry;
      }
      return this.#refreshVolatileIntent(entry, entry.idempotencyKey);
    });
  }

  freezeRequest(
    id: number,
    target: {
      readonly storeId: string;
      readonly documentId: string;
      readonly expectedStructuredHeadSha256: string;
    },
  ): Promise<CoworkPasteProvenanceOutboxEntry> {
    return this.#replace(id, (entry) => {
      if (entry.frozenRequest !== undefined) return entry;
      if (entry.sourceKind !== "paste" && entry.capturedActor === undefined) {
        throw new Error(
          "A direct or manual provenance request has no capture-time actor binding.",
        );
      }
      return {
        ...entry,
        frozenRequest: {
          storeId: target.storeId,
          documentId: target.documentId,
          ...(entry.capturedActor === undefined
            ? {}
            : {
                expectedActorRef: entry.capturedActor.ref,
                expectedActorIdentityStatus:
                  entry.capturedActor.identity_status,
              }),
          sourceKind: entry.sourceKind,
          basisKind: entry.basisKind,
          expectedStructuredHeadSha256: target.expectedStructuredHeadSha256,
          anchor: entry.anchor,
          attestation: entry.determination,
          idempotencyKey: entry.idempotencyKey,
        },
      };
    });
  }

  markFailure(
    id: number,
    failure: CoworkPasteProvenanceFailure,
  ): Promise<CoworkPasteProvenanceOutboxEntry> {
    return this.#replace(id, (entry) => ({
      ...entry,
      status:
        failure.kind === "stale_target"
          ? "stale_target"
          : failure.kind === "terminal"
            ? "terminal_failure"
            : "retryable_failure",
      failure,
    }));
  }

  resetAfterActorChange(
    idempotencyKeyPrefix: string,
    determination: CoworkProvenanceDetermination,
  ): Promise<readonly CoworkPasteProvenanceOutboxEntry[]> {
    if (
      idempotencyKeyPrefix.length === 0 ||
      idempotencyKeyPrefix.length > 100
    ) {
      return Promise.reject(
        new Error("Actor-change recovery requires a bounded key prefix"),
      );
    }
    return this.#update((current) => {
      const priorIdempotencyKeys = current.entries.map(
        (entry) => entry.idempotencyKey,
      );
      const entries = current.entries.map(
        (entry): CoworkPasteProvenanceOutboxEntry => ({
          ...entry,
          idempotencyKey: `${idempotencyKeyPrefix}:${String(entry.id)}`,
          // A stale automatic direct-entry assertion must not be resent as
          // though it were still machine-observed under the new actor. It is
          // now an explicit, legacy/manual determination. Paste keeps its
          // actual input source while likewise requiring a fresh attestation.
          sourceKind:
            entry.sourceKind === "direct_entry" ? "legacy" : entry.sourceKind,
          basisKind: "user_attestation",
          determination,
          status: "awaiting_determination",
          requiresExplicitDetermination: true,
          frozenRequest: undefined,
          failure: {
            code: COWORK_PROVENANCE_ACTOR_CHANGED,
            message:
              "The acting identity changed before this attribution was saved.",
            kind: "terminal",
          },
        }),
      );
      return {
        record: { ...current, entries },
        result: {
          entries: entries.map(cloneEntry),
          priorIdempotencyKeys,
        },
      };
    }).then(({ entries, priorIdempotencyKeys }) =>
      entries.map((entry, index) =>
        this.#refreshVolatileIntent(
          entry,
          priorIdempotencyKeys[index] ?? entry.idempotencyKey,
        ),
      ),
    );
  }

  retarget(
    id: number,
    idempotencyKey: string,
    determination: CoworkProvenanceDetermination,
  ): Promise<CoworkPasteProvenanceOutboxEntry> {
    let priorIdempotencyKey = idempotencyKey;
    return this.#replace(id, (entry) => {
      priorIdempotencyKey = entry.idempotencyKey;
      if (
        entry.status !== "stale_target" &&
        entry.status !== "terminal_failure"
      ) {
        throw new Error(
          "Only an explicitly rejected paste provenance target can be replaced",
        );
      }
      return {
        ...entry,
        idempotencyKey,
        determination,
        status: "ready",
        frozenRequest: undefined,
        failure: undefined,
      };
    }).then((entry) => this.#refreshVolatileIntent(entry, priorIdempotencyKey));
  }

  remove(id: number): Promise<void> {
    return this.#update((current) => {
      const removed = current.entries.find((entry) => entry.id === id);
      return {
        record: {
          ...current,
          entries: current.entries.filter((entry) => entry.id !== id),
        },
        result: removed?.idempotencyKey,
      };
    }).then((idempotencyKey) => {
      if (idempotencyKey === undefined) return;
      // `remove` is called only after a confirmed server receipt. This is the
      // point at which even the volatile-backing recovery journal may retire.
      this.#intentStage.remove(this.#key, idempotencyKey);
    });
  }

  #replace(
    id: number,
    replacement: (
      entry: CoworkPasteProvenanceOutboxEntry,
    ) => CoworkPasteProvenanceOutboxEntry,
  ): Promise<CoworkPasteProvenanceOutboxEntry> {
    return this.#update((current) => {
      const found = current.entries.find((entry) => entry.id === id);
      if (found === undefined) {
        throw new Error("Paste provenance outbox entry was not found");
      }
      const next = replacement(cloneEntry(found));
      return {
        record: {
          ...current,
          entries: current.entries.map((entry) =>
            entry.id === id ? next : entry,
          ),
        },
        result: cloneEntry(next),
      };
    });
  }

  #update<Value>(
    mutation: (current: PersistedOutbox) => {
      readonly record: PersistedOutbox;
      readonly result: Value;
    },
  ): Promise<Value> {
    return this.#enqueue(() => this.#backing.mutate(this.#key, mutation));
  }

  #refreshVolatileIntent(
    entry: CoworkPasteProvenanceOutboxEntry,
    priorIdempotencyKey: string,
  ): CoworkPasteProvenanceOutboxEntry {
    if (
      this.#backing.durable ||
      (entry.status !== "awaiting_determination" && entry.status !== "ready")
    ) {
      return entry;
    }
    this.#intentStage.put(this.#key, {
      anchor: entry.anchor,
      idempotencyKey: entry.idempotencyKey,
      substantial: entry.substantial,
      sourceKind: entry.sourceKind,
      basisKind: entry.basisKind,
      determination: entry.determination,
      ...(entry.capturedActor === undefined
        ? {}
        : { capturedActor: entry.capturedActor }),
      capturedAt: entry.capturedAt,
      passageExcerpt: entry.passageExcerpt,
      ...(entry.capturedBaseStructuredHeadSha256 === undefined
        ? {}
        : {
            capturedBaseStructuredHeadSha256:
              entry.capturedBaseStructuredHeadSha256,
          }),
      ...(entry.requiresExplicitDetermination === true
        ? { requiresExplicitDetermination: true }
        : {}),
      status:
        entry.status === "awaiting_determination"
          ? "awaiting_determination"
          : "ready",
    });
    if (priorIdempotencyKey !== entry.idempotencyKey) {
      this.#intentStage.remove(this.#key, priorIdempotencyKey);
    }
    return entry;
  }

  async #reconcileStagedIntents(): Promise<void> {
    const staged = this.#intentStage.list(this.#key);
    for (const capture of staged) {
      await this.#backing.mutate(this.#key, (current) => {
        const existing = current.entries.find(
          (entry) => entry.idempotencyKey === capture.idempotencyKey,
        );
        if (existing !== undefined) {
          if (
            capture.status === "capturing" &&
            existing.status === "capturing" &&
            existing.frozenRequest === undefined
          ) {
            const replacement: CoworkPasteProvenanceOutboxEntry = {
              id: existing.id,
              ...capture,
              sourceKind: capture.sourceKind ?? "paste",
              capturedAt: capture.capturedAt ?? existing.capturedAt,
              passageExcerpt:
                capture.passageExcerpt ??
                coworkPastePassageExcerpt(capture.anchor.exact),
            };
            return {
              record: {
                ...current,
                entries: current.entries.map((entry) =>
                  entry.id === existing.id ? replacement : entry,
                ),
              },
              result: undefined,
            };
          }
          // A ready or frozen row is an immutable retry contract. A stale
          // synchronous journal from before that close may never reopen it.
          return { record: current, result: undefined };
        }
        const entry: CoworkPasteProvenanceOutboxEntry = {
          id: current.nextId,
          ...capture,
          sourceKind: capture.sourceKind ?? "paste",
          capturedAt: capture.capturedAt ?? new Date(0).toISOString(),
          passageExcerpt:
            capture.passageExcerpt ??
            coworkPastePassageExcerpt(capture.anchor.exact),
        };
        return {
          record: {
            ...current,
            nextId: current.nextId + 1,
            entries: [...current.entries, entry],
          },
          result: undefined,
        };
      });
      if (this.#backing.durable) {
        this.#intentStage.remove(this.#key, capture.idempotencyKey);
      }
    }
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
