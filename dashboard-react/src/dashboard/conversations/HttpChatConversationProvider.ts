// The React dashboard's live house-conversation transport. It implements the
// transport-agnostic ChatConversationProvider seam over the same HTTP surface
// the legacy chat sidebar drives: GET /api/conversations/<id> for the snapshot,
// POST .../respond for a human turn, and a 3s poll loop for invalidation. It
// reuses normalizeConversationPayload so conversation_* mapping stays in one
// tested place.
//
// One instance binds one conversation, exactly like InMemoryChatProvider, so a
// call for another conversation id is a programming error rather than a silent
// cross-load. The provider performs the only I/O in this module. App-specific
// linkage belongs beside each consumer, not in this adapter.

import {
  normalizeConversationPayload,
  type ChatConversationProvider,
  type ChatConversationSnapshot,
  type ChatInvalidationListener,
  type ChatSendInput,
  type ChatUnsubscribe,
  type RawChatConversationPayload,
} from "../../widget-library/chat";

/** The house conversation poll cadence (chat_sidebar and the tabs poll loop). */
const DEFAULT_POLL_INTERVAL_MS = 3000;
const DEFAULT_BASE_PATH = "/api/conversations";

export interface HttpChatConfig {
  /** The single conversation this provider is bound to. */
  readonly conversationId: string;
  /** Injectable fetch, defaulting to the global. Tests pass a mock. */
  readonly fetchImpl?: typeof fetch;
  /**
   * Poll cadence in ms for the subscribe invalidation loop. Defaults to the
   * house 3s conversation loop. A value <= 0 disables polling (the consumer
   * still loads once and reloads on send), which keeps timer-free tests simple.
   */
  readonly pollIntervalMs?: number;
  /** Base path of the house conversation surface. Defaults to /api/conversations. */
  readonly basePath?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isConversationPayload(
  value: unknown,
): value is RawChatConversationPayload {
  return (
    isRecord(value) &&
    isRecord(value.conversation) &&
    typeof value.conversation.conversation_id === "string"
  );
}

/** Prefer the server's own error text, else a stable fallback for this path. */
function errorText(
  payload: unknown,
  response: Response,
  fallback: string,
): string {
  if (isRecord(payload) && typeof payload.error === "string" && payload.error) {
    return payload.error;
  }
  if (response.statusText) return `${fallback} (${response.statusText})`;
  return fallback;
}

export class HttpChatConversationProvider implements ChatConversationProvider {
  private readonly conversationId: string;
  private readonly injectedFetch: typeof fetch | undefined;
  private readonly pollIntervalMs: number;
  private readonly basePath: string;
  private readonly listeners = new Set<ChatInvalidationListener>();
  private timer: ReturnType<typeof setInterval> | null = null;
  private lastSnapshot: ChatConversationSnapshot | null = null;
  private requestSequence = 0;
  private cachedSequence = 0;

  constructor(config: HttpChatConfig) {
    this.conversationId = config.conversationId;
    this.injectedFetch = config.fetchImpl;
    this.pollIntervalMs = config.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
    this.basePath = config.basePath ?? DEFAULT_BASE_PATH;
  }

  // Resolved at call time so a missing global fetch is a clear runtime error at
  // the boundary rather than a construction-time throw on an unbound global.
  private fetcher(): typeof fetch {
    if (this.injectedFetch !== undefined) return this.injectedFetch;
    const global = globalThis.fetch;
    if (typeof global !== "function") {
      throw new Error("global fetch is unavailable, so inject fetchImpl");
    }
    return global.bind(globalThis);
  }

  private endpoint(suffix = ""): string {
    return `${this.basePath}/${encodeURIComponent(this.conversationId)}${suffix}`;
  }

  private assertBound(conversationId: string): void {
    if (conversationId !== this.conversationId) {
      throw new Error(
        `This provider is bound to ${this.conversationId}, not ${conversationId}`,
      );
    }
  }

  private async readJson(response: Response): Promise<unknown> {
    try {
      return await response.json();
    } catch {
      // A non-JSON body (e.g. an HTML error page) is treated as no payload, so
      // the status-derived fallback message drives the thrown error.
      return undefined;
    }
  }

  async loadConversation(
    conversationId: string,
  ): Promise<ChatConversationSnapshot> {
    this.assertBound(conversationId);
    const sequence = ++this.requestSequence;
    const response = await this.fetcher()(this.endpoint(), {
      method: "GET",
      headers: { Accept: "application/json" },
    });
    const payload = await this.readJson(response);
    if (!response.ok || !isConversationPayload(payload)) {
      throw new Error(errorText(payload, response, "Conversation could not load."));
    }
    if (payload.conversation.conversation_id !== this.conversationId) {
      throw new Error("Chat returned the wrong conversation.");
    }
    const snapshot = normalizeConversationPayload(payload);
    if (sequence >= this.cachedSequence) {
      this.cachedSequence = sequence;
      this.lastSnapshot = snapshot;
    }
    return snapshot;
  }

  async sendMessage(
    conversationId: string,
    input: ChatSendInput,
  ): Promise<ChatConversationSnapshot> {
    this.assertBound(conversationId);
    const response = await this.fetcher()(this.endpoint("/respond"), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      // Structured replies carry the exact pending-question message id. A
      // plain composer turn omits it and remains an ordinary user message.
      body: JSON.stringify({
        value: input.value,
        ...(input.inReplyTo === undefined
          ? {}
          : { in_reply_to: input.inReplyTo }),
        ...(input.context === undefined
          ? {}
          : {
              context: {
                kind: input.context.kind,
                action_snapshot_id: input.context.actionSnapshotId,
                store_id: input.context.storeId,
                document_id: input.context.documentId,
              },
            }),
      }),
    });
    const payload = await this.readJson(response);
    const failed =
      !response.ok ||
      (isRecord(payload) && typeof payload.error === "string" && payload.error);
    if (failed) {
      throw new Error(
        errorText(payload, response, "Message could not be delivered."),
      );
    }
    const messageId =
      isRecord(payload) &&
      typeof payload.message_id === "string" &&
      payload.message_id.trim().length > 0
        ? payload.message_id
        : null;
    if (messageId === null) {
      throw new Error(
        "Your message may have been delivered, but chat could not confirm it. Wait for chat to refresh before trying again.",
      );
    }

    // The POST acknowledgement is authoritative: once it returns a message id,
    // a transient follow-up GET failure must not retain the draft and invite a
    // duplicate send. Block older in-flight loads from regressing the cache,
    // then prefer the server snapshot and fall back to an optimistic user turn.
    const acknowledgementSequence = ++this.requestSequence;
    this.cachedSequence = Math.max(
      this.cachedSequence,
      acknowledgementSequence,
    );
    try {
      return await this.loadConversation(conversationId);
    } catch {
      const base =
        this.lastSnapshot ?? {
          conversationId: this.conversationId,
          status: "open",
          agentLiveness: "unknown",
          messages: [],
        };
      if (base.messages.some((message) => message.id === messageId)) {
        return base;
      }
      const optimistic: ChatConversationSnapshot = {
        ...base,
        messages: [
          ...base.messages,
          {
            id: messageId,
            author: "user",
            content: input.value,
            context: input.context,
          },
        ],
      };
      this.requestSequence += 1;
      this.cachedSequence = this.requestSequence;
      this.lastSnapshot = optimistic;
      return optimistic;
    }
  }

  subscribe(
    conversationId: string,
    onInvalidate: ChatInvalidationListener,
  ): ChatUnsubscribe {
    this.assertBound(conversationId);
    this.listeners.add(onInvalidate);
    this.startPolling();
    return () => {
      this.listeners.delete(onInvalidate);
      if (this.listeners.size === 0) this.stopPolling();
    };
  }

  /** Ask mounted consumers to reload immediately without replacing the provider. */
  invalidate(): void {
    // Snapshot the set so an unsubscribe during dispatch remains well defined.
    for (const listener of [...this.listeners]) listener();
  }

  private startPolling(): void {
    if (this.timer !== null || this.pollIntervalMs <= 0) return;
    this.timer = setInterval(() => {
      this.invalidate();
    }, this.pollIntervalMs);
  }

  private stopPolling(): void {
    if (this.timer === null) return;
    clearInterval(this.timer);
    this.timer = null;
  }
}

/** Build the live dashboard provider for one house conversation. */
export function createHttpChatProvider(
  config: HttpChatConfig,
): HttpChatConversationProvider {
  return new HttpChatConversationProvider(config);
}
