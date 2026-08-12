import {
  ChatExecutionSelectionError,
  type ChatExecutionModelOption,
  type ChatExecutionProfileProvider,
  type ChatExecutionProviderOption,
  type ChatExecutionSelectionInput,
  type ChatExecutionSnapshot,
  type ChatInvalidationListener,
  type ChatUnsubscribe,
} from "../../widget-library/chat";

type JsonRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is JsonRecord =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const array = (value: unknown): readonly unknown[] =>
  Array.isArray(value) ? value : [];

function optionalString(record: JsonRecord, ...names: string[]): string | undefined {
  for (const name of names) {
    const value = record[name];
    if (typeof value === "string" && value.trim().length > 0) return value;
  }
  return undefined;
}

function requiredString(record: JsonRecord, ...names: string[]): string {
  const value = optionalString(record, ...names);
  if (value !== undefined) return value;
  throw new Error(`Missing execution field: ${names[0]}`);
}

function availabilityReason(record: JsonRecord): string | undefined {
  const direct = optionalString(
    record,
    "unavailable_reason",
    "unavailableReason",
    "reason",
  );
  if (direct !== undefined) return direct;
  if (
    typeof record.availability === "string" &&
    record.availability !== "available"
  ) {
    return record.availability;
  }
  if (isRecord(record.availability)) {
    return optionalString(record.availability, "message", "reason");
  }
  return undefined;
}

function normalizeModel(value: unknown): ChatExecutionModelOption {
  if (!isRecord(value)) throw new Error("Execution model must be an object.");
  return {
    id: requiredString(value, "id", "model_id", "modelId"),
    label:
      optionalString(value, "label", "model_label", "modelLabel") ??
      requiredString(value, "id", "model_id", "modelId"),
    available: value.available !== false,
    description: optionalString(value, "description"),
    unavailableReason: availabilityReason(value),
  };
}

function normalizeProvider(value: unknown): ChatExecutionProviderOption {
  if (!isRecord(value)) {
    throw new Error("Execution provider must be an object.");
  }
  return {
    id: requiredString(value, "id", "provider_id", "providerId"),
    label:
      optionalString(value, "label", "provider_label", "providerLabel") ??
      requiredString(value, "id", "provider_id", "providerId"),
    available: value.available !== false,
    authMode: optionalString(value, "auth_mode", "authMode"),
    description: optionalString(value, "description"),
    unavailableReason: availabilityReason(value),
    models: array(value.models).map(normalizeModel),
  };
}

/**
 * Normalize the backend's snake_case execution projection. A flat selection is
 * accepted as a compatibility fallback, but catalog labels remain
 * server-authored and IDs remain opaque.
 */
export function normalizeChatExecutionSnapshot(
  value: unknown,
): ChatExecutionSnapshot {
  if (!isRecord(value)) {
    throw new Error("Execution profile must be an object.");
  }
  const rawSelection = isRecord(value.selection) ? value.selection : value;
  const rawCatalog = isRecord(value.catalog) ? value.catalog : value;
  const providers = array(rawCatalog.providers).map(normalizeProvider);
  const providerId = requiredString(
    rawSelection,
    "provider_id",
    "providerId",
  );
  const modelId = requiredString(rawSelection, "model_id", "modelId");
  const provider = providers.find((candidate) => candidate.id === providerId);
  const model = provider?.models.find((candidate) => candidate.id === modelId);
  return {
    selection: {
      providerId,
      modelId,
      providerLabel:
        optionalString(
          rawSelection,
          "provider_label",
          "providerLabel",
        ) ??
        provider?.label ??
        providerId,
      modelLabel:
        optionalString(rawSelection, "model_label", "modelLabel") ??
        model?.label ??
        modelId,
      revision:
        typeof rawSelection.revision === "string"
          ? rawSelection.revision
          : typeof value.revision === "string"
            ? value.revision
            : typeof value.execution_revision === "string"
              ? value.execution_revision
              : (() => {
                  throw new Error("Missing execution field: revision");
                })(),
    },
    providers,
    readOnly: value.read_only === true || value.readOnly === true,
  };
}

export interface HttpChatExecutionEnvelope {
  readonly execution: ChatExecutionSnapshot;
  /** Feature-owned lifecycle data carried beside the generic execution shape. */
  readonly agent?: unknown;
}

export function normalizeChatExecutionEnvelope(
  payload: unknown,
): HttpChatExecutionEnvelope {
  if (!isRecord(payload)) {
    throw new Error("Execution response must be an object.");
  }
  const rawExecution = payload.execution ?? payload;
  return {
    execution: normalizeChatExecutionSnapshot(rawExecution),
    agent: payload.agent,
  };
}

function errorMessage(payload: unknown, fallback: string): string {
  if (!isRecord(payload)) return fallback;
  if (typeof payload.message === "string" && payload.message.trim().length > 0) {
    return payload.message;
  }
  if (typeof payload.error === "string" && payload.error.trim().length > 0) {
    return payload.error;
  }
  if (isRecord(payload.error)) {
    return optionalString(payload.error, "message") ?? fallback;
  }
  return fallback;
}

const sameModel = (
  left: ChatExecutionModelOption,
  right: ChatExecutionModelOption,
): boolean =>
  left.id === right.id &&
  left.label === right.label &&
  left.available === right.available &&
  left.description === right.description &&
  left.unavailableReason === right.unavailableReason;

const sameProvider = (
  left: ChatExecutionProviderOption,
  right: ChatExecutionProviderOption,
): boolean =>
  left.id === right.id &&
  left.label === right.label &&
  left.available === right.available &&
  left.authMode === right.authMode &&
  left.description === right.description &&
  left.unavailableReason === right.unavailableReason &&
  left.models.length === right.models.length &&
  left.models.every((model, index) =>
    sameModel(model, right.models[index]),
  );

const sameSnapshot = (
  left: ChatExecutionSnapshot | null,
  right: ChatExecutionSnapshot,
): boolean =>
  left !== null &&
  left.selection.providerId === right.selection.providerId &&
  left.selection.modelId === right.selection.modelId &&
  left.selection.providerLabel === right.selection.providerLabel &&
  left.selection.modelLabel === right.selection.modelLabel &&
  left.selection.revision === right.selection.revision &&
  left.readOnly === right.readOnly &&
  left.providers.length === right.providers.length &&
  left.providers.every((provider, index) =>
    sameProvider(provider, right.providers[index]),
  );

export interface HttpChatExecutionProfileConfig {
  readonly targetId: string;
  readonly loadUrl: string;
  readonly selectUrl: string;
  readonly initialSnapshot?: ChatExecutionSnapshot;
  readonly fetchImpl?: typeof fetch;
  /** Feature-owned exact-action authority; omitted for ordinary chats. */
  readonly authorizeSelect?: (
    body: Record<string, unknown>,
  ) => Promise<Record<string, string>>;
  /** Lets a feature adopt lifecycle data returned in the same transaction. */
  readonly onEnvelope?: (envelope: HttpChatExecutionEnvelope) => void;
}

interface PendingExecutionRead {
  readonly sequence: number;
  readonly promise: Promise<ChatExecutionSnapshot>;
}

/**
 * Neutral same-origin HTTP adapter. Endpoint ownership remains with the host;
 * this class only normalizes execution envelopes and sends an atomic pair.
 */
export class HttpChatExecutionProfileProvider
  implements ChatExecutionProfileProvider
{
  readonly #targetId: string;
  readonly #loadUrl: string;
  readonly #selectUrl: string;
  readonly #injectedFetch: typeof fetch | undefined;
  readonly #authorizeSelect:
    | ((body: Record<string, unknown>) => Promise<Record<string, string>>)
    | undefined;
  readonly #onEnvelope:
    | ((envelope: HttpChatExecutionEnvelope) => void)
    | undefined;
  readonly #listeners = new Set<ChatInvalidationListener>();
  #snapshot: ChatExecutionSnapshot | null;
  #refreshRequested = false;
  #authoritySequence = 0;
  #readSequence = 0;
  #latestRead: PendingExecutionRead | null = null;

  constructor(config: HttpChatExecutionProfileConfig) {
    this.#targetId = config.targetId;
    this.#loadUrl = config.loadUrl;
    this.#selectUrl = config.selectUrl;
    this.#injectedFetch = config.fetchImpl;
    this.#authorizeSelect = config.authorizeSelect;
    this.#onEnvelope = config.onEnvelope;
    this.#snapshot = config.initialSnapshot ?? null;
  }

  #fetcher(): typeof fetch {
    if (this.#injectedFetch !== undefined) return this.#injectedFetch;
    if (typeof globalThis.fetch !== "function") {
      throw new Error("global fetch is unavailable, so inject fetchImpl");
    }
    return globalThis.fetch.bind(globalThis);
  }

  #assertTarget(targetId: string): void {
    if (targetId !== this.#targetId) {
      throw new Error(
        `This execution provider is bound to ${this.#targetId}, not ${targetId}`,
      );
    }
  }

  async #json(response: Response): Promise<unknown> {
    try {
      return await response.json();
    } catch {
      return undefined;
    }
  }

  #adopt(envelope: HttpChatExecutionEnvelope): ChatExecutionSnapshot {
    this.#snapshot = envelope.execution;
    this.#onEnvelope?.(envelope);
    return envelope.execution;
  }

  /**
   * Fence reads that began before a newer server or host-authoritative
   * projection. A late GET may resolve to the current snapshot, but it must
   * never adopt its stale envelope or notify the host with stale lifecycle
   * data.
   */
  #supersedePendingReads(): void {
    this.#authoritySequence += 1;
  }

  /** Reconcile an execution projection already loaded by a containing feature. */
  replaceSnapshot(snapshot: ChatExecutionSnapshot): void {
    this.#supersedePendingReads();
    if (sameSnapshot(this.#snapshot, snapshot)) return;
    this.#snapshot = snapshot;
    for (const listener of [...this.#listeners]) listener();
  }

  /** Ask mounted consumers to reconcile after an external server event. */
  invalidate(): void {
    this.#supersedePendingReads();
    this.#snapshot = null;
    for (const listener of [...this.#listeners]) listener();
  }

  refresh(targetId: string): void {
    this.#assertTarget(targetId);
    this.#supersedePendingReads();
    this.#snapshot = null;
    this.#refreshRequested = true;
  }

  async load(targetId: string): Promise<ChatExecutionSnapshot> {
    this.#assertTarget(targetId);
    if (this.#snapshot !== null) return this.#snapshot;
    const authoritySequence = this.#authoritySequence;
    const readSequence = ++this.#readSequence;
    const loadUrl = this.#refreshRequested
      ? `${this.#loadUrl}${this.#loadUrl.includes("?") ? "&" : "?"}refresh_execution=1`
      : this.#loadUrl;
    const promise = (async (): Promise<ChatExecutionSnapshot> => {
      const response = await this.#fetcher()(loadUrl, {
        headers: { Accept: "application/json" },
      });
      const payload = await this.#json(response);
      if (!response.ok) {
        throw new Error(
          errorMessage(
            payload,
            "Provider and model choices could not be loaded.",
          ),
        );
      }
      const envelope = normalizeChatExecutionEnvelope(payload);
      if (
        authoritySequence === this.#authoritySequence &&
        this.#latestRead?.sequence === readSequence
      ) {
        const snapshot = this.#adopt(envelope);
        this.#refreshRequested = false;
        return snapshot;
      }

      if (this.#snapshot !== null) return this.#snapshot;
      const latestRead = this.#latestRead;
      if (
        latestRead !== null &&
        latestRead.sequence !== readSequence
      ) {
        return latestRead.promise;
      }

      // An invalidation or refresh can fence the only in-flight read before
      // its replacement is issued. Start that replacement here instead of
      // leaking the superseded projection to the caller.
      if (this.#latestRead?.sequence === readSequence) {
        this.#latestRead = null;
      }
      return this.load(targetId);
    })();
    this.#latestRead = { sequence: readSequence, promise };
    try {
      return await promise;
    } finally {
      if (this.#latestRead?.sequence === readSequence) {
        this.#latestRead = null;
      }
    }
  }

  async select(
    targetId: string,
    selection: ChatExecutionSelectionInput,
  ): Promise<ChatExecutionSnapshot> {
    this.#assertTarget(targetId);
    const body = {
      provider_id: selection.providerId,
      model_id: selection.modelId,
      expected_revision: selection.expectedRevision,
    };
    const authorityHeaders = await this.#authorizeSelect?.(body);
    const response = await this.#fetcher()(this.#selectUrl, {
      method: "PATCH",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...authorityHeaders,
      },
      body: JSON.stringify(body),
    });
    const payload = await this.#json(response);
    if (response.status === 409) {
      let authoritative: ChatExecutionSnapshot | undefined;
      try {
        const envelope = normalizeChatExecutionEnvelope(payload);
        this.#supersedePendingReads();
        authoritative = this.#adopt(envelope);
      } catch {
        this.#supersedePendingReads();
        this.#snapshot = null;
        try {
          authoritative = await this.load(targetId);
        } catch {
          authoritative = undefined;
        }
      }
      throw new ChatExecutionSelectionError(
        errorMessage(
          payload,
          "The model selection changed elsewhere. The latest selection is shown.",
        ),
        authoritative,
      );
    }
    if (!response.ok) {
      throw new ChatExecutionSelectionError(
        errorMessage(payload, "The model selection could not be changed."),
      );
    }
    const envelope = normalizeChatExecutionEnvelope(payload);
    this.#supersedePendingReads();
    return this.#adopt(envelope);
  }

  subscribe(
    targetId: string,
    onInvalidate: ChatInvalidationListener,
  ): ChatUnsubscribe {
    this.#assertTarget(targetId);
    this.#listeners.add(onInvalidate);
    return () => this.#listeners.delete(onInvalidate);
  }
}

export function createHttpChatExecutionProfileProvider(
  config: HttpChatExecutionProfileConfig,
): HttpChatExecutionProfileProvider {
  return new HttpChatExecutionProfileProvider(config);
}
