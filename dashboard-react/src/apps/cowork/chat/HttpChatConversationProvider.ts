// The live document-conversation transport. It implements the house
// ChatConversationProvider seam over the same conversation HTTP surface the
// legacy chat sidebar drives: GET /api/conversations/<id> for the snapshot,
// POST .../respond for a human turn, and a 3s poll loop for the invalidation
// signal (the house conversation cadence). It replaces createDemoChatProvider
// as the seam the surface wires in, and it re-uses normalizeConversationPayload
// so the mirroring of conversation_* semantics stays in the one tested place.
//
// One instance binds one conversation, exactly like InMemoryChatProvider, so a
// call for another conversation id is a programming error rather than a silent
// cross-load. The provider performs the only I/O in this module and holds no
// document linkage: feedback span links and routing deliveries live in the
// annotations store beside it.

import {
  normalizeConversationPayload,
  type ChatConversationProvider,
  type ChatConversationSnapshot,
  type ChatInvalidationListener,
  type ChatSendInput,
  type ChatUnsubscribe,
  type RawChatConversationPayload,
} from "../../../widget-library/chat";

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
    const response = await this.fetcher()(this.endpoint(), {
      method: "GET",
      headers: { Accept: "application/json" },
    });
    const payload = await this.readJson(response);
    if (!response.ok || !isConversationPayload(payload)) {
      throw new Error(errorText(payload, response, "Conversation could not load."));
    }
    return normalizeConversationPayload(payload);
  }

  async sendMessage(
    conversationId: string,
    input: ChatSendInput,
  ): Promise<ChatConversationSnapshot> {
    this.assertBound(conversationId);
    const response = await this.fetcher()(this.endpoint("/respond"), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      // The respond route answers a pending question or appends a general user
      // message from the one value field. inReplyTo is a client-side hint the
      // house route does not consume, so only value crosses the wire.
      body: JSON.stringify({ value: input.value }),
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
    // The respond route returns only an ack (message_id), so the next snapshot
    // comes from a reload. A transient reload failure surfaces as a send error
    // and the poll loop reconciles the successfully posted turn on its next tick.
    return this.loadConversation(conversationId);
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

  private startPolling(): void {
    if (this.timer !== null || this.pollIntervalMs <= 0) return;
    this.timer = setInterval(() => {
      // A snapshot of the set so an unsubscribe during dispatch is well defined.
      for (const listener of [...this.listeners]) listener();
    }, this.pollIntervalMs);
  }

  private stopPolling(): void {
    if (this.timer === null) return;
    clearInterval(this.timer);
    this.timer = null;
  }
}

/** Build the live document-conversation provider for one conversation. */
export function createHttpChatProvider(
  config: HttpChatConfig,
): HttpChatConversationProvider {
  return new HttpChatConversationProvider(config);
}
