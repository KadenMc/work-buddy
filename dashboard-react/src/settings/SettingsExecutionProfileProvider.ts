import {
  ChatExecutionSelectionError,
  type ChatExecutionProfileProvider,
  type ChatExecutionSelectionInput,
  type ChatExecutionSnapshot,
  type ChatInvalidationListener,
  type ChatUnsubscribe,
} from "../widget-library/chat";
import { normalizeChatExecutionSnapshot } from "../dashboard/conversations/HttpChatExecutionProfileProvider";
import type { EffectiveSettingValue, SettingId } from "./contracts";
import { isChatExecutionSettingValue } from "./executionProfile";
import { patchSettingValue, SettingsServerError } from "./serverSettings";

interface SettingsExecutionProfileConfig {
  readonly settingId: SettingId;
  /** The containing Settings page remains the sole value projection. */
  readonly getValue: () => EffectiveSettingValue | undefined;
  readonly getReadOnly: () => boolean;
  readonly adoptValue: (value: EffectiveSettingValue) => void;
  readonly fetchImpl?: typeof fetch;
}

/** Shared Chat picker adapter; all default writes use the canonical Settings API. */
export class SettingsExecutionProfileProvider implements ChatExecutionProfileProvider {
  readonly #config: SettingsExecutionProfileConfig;
  readonly #listeners = new Set<ChatInvalidationListener>();
  #catalog: Record<string, unknown> | null = null;
  #catalogRequest: Promise<Record<string, unknown>> | null = null;
  #catalogSequence = 0;
  #refresh = false;

  constructor(config: SettingsExecutionProfileConfig) {
    this.#config = config;
  }

  #fetcher(): typeof fetch {
    return this.#config.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  #assertTarget(targetId: string): void {
    if (targetId !== this.#config.settingId) throw new Error("This model picker belongs to a different setting.");
  }

  async #loadCatalog(): Promise<Record<string, unknown>> {
    if (this.#catalog !== null) return this.#catalog;
    if (this.#catalogRequest !== null) return this.#catalogRequest;
    const sequence = this.#catalogSequence;
    const pending = (async () => {
      const response = await this.#fetcher()(`/api/settings/execution-catalog${this.#refresh ? "?refresh=1" : ""}`, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Provider and model choices could not be loaded.");
      const payload: unknown = await response.json();
      if (typeof payload !== "object" || payload === null || !Array.isArray((payload as Record<string, unknown>).providers)) {
        throw new Error("The model catalog was not valid.");
      }
      if (sequence !== this.#catalogSequence) return this.#loadCatalog();
      this.#catalog = payload as Record<string, unknown>;
      this.#refresh = false;
      return this.#catalog;
    })();
    this.#catalogRequest = pending;
    try {
      return await pending;
    } finally {
      if (this.#catalogRequest === pending) this.#catalogRequest = null;
    }
  }

  #project(value: EffectiveSettingValue): ChatExecutionSnapshot {
    if (!isChatExecutionSettingValue(value.effectiveValue)) throw new Error("The saved default chat model needs attention.");
    return normalizeChatExecutionSnapshot({
      selection: { ...value.effectiveValue, revision: value.revision },
      providers: this.#catalog?.providers ?? [],
      read_only: this.#config.getReadOnly(),
    });
  }

  async load(targetId: string): Promise<ChatExecutionSnapshot> {
    this.#assertTarget(targetId);
    await this.#loadCatalog();
    // Read after discovery: a late catalog response must not restore an old value.
    const value = this.#config.getValue();
    if (value === undefined) throw new Error("The authoritative default is not available.");
    return this.#project(value);
  }

  async select(targetId: string, selection: ChatExecutionSelectionInput): Promise<ChatExecutionSnapshot> {
    this.#assertTarget(targetId);
    if (this.#config.getReadOnly()) throw new ChatExecutionSelectionError("Dashboard settings are read-only.");
    try {
      const result = await patchSettingValue(this.#config.settingId, {
        provider_id: selection.providerId,
        model_id: selection.modelId,
      }, selection.expectedRevision, this.#fetcher());
      this.#config.adoptValue(result.value);
      return this.#project(this.#newestValue(result.value));
    } catch (cause) {
      if (cause instanceof SettingsServerError && cause.authoritativeValue !== undefined) {
        this.#config.adoptValue(cause.authoritativeValue);
        throw new ChatExecutionSelectionError(cause.message, this.#project(this.#newestValue(cause.authoritativeValue)));
      }
      throw new ChatExecutionSelectionError(cause instanceof Error ? cause.message : "The default chat model could not be changed.");
    }
  }

  #newestValue(incoming: EffectiveSettingValue): EffectiveSettingValue {
    const current = this.#config.getValue();
    const currentRevision = current?.revision.match(/^value:(\d+)$/);
    const incomingRevision = incoming.revision.match(/^value:(\d+)$/);
    return current !== undefined && currentRevision && incomingRevision && Number(currentRevision[1]) > Number(incomingRevision[1]) ? current : incoming;
  }

  /** Settings/SSE owns values; invalidation only asks the shared picker to project again. */
  invalidate(): void {
    for (const listener of [...this.#listeners]) listener();
  }

  refresh(targetId: string): void {
    this.#assertTarget(targetId);
    this.#catalogSequence += 1;
    this.#catalog = null;
    this.#catalogRequest = null;
    this.#refresh = true;
  }

  subscribe(targetId: string, listener: ChatInvalidationListener): ChatUnsubscribe {
    this.#assertTarget(targetId);
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }
}
