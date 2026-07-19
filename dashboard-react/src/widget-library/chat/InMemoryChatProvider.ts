// In-memory fixture implementation of the ChatConversationProvider seam. Used by
// component and hook tests and available to a development harness. It performs
// no I/O and holds one conversation of scripted state. It is deliberately NOT a
// live transport, which is supplied separately behind the same seam.

import type {
  ChatAgentLiveness,
  ChatConversationProvider,
  ChatConversationSnapshot,
  ChatConversationStatus,
  ChatInvalidationListener,
  ChatMessage,
  ChatSendInput,
  ChatUnsubscribe,
} from "./contracts";

export interface InMemoryChatSeed {
  readonly conversationId: string;
  readonly title?: string;
  readonly status?: ChatConversationStatus;
  readonly agentLiveness?: ChatAgentLiveness;
  readonly messages?: readonly ChatMessage[];
  /**
   * Optional scripted agent reply. Given the human input and the snapshot after
   * the human turn was appended, returns agent or system messages to append.
   */
  readonly autoReply?: (
    input: ChatSendInput,
    snapshot: ChatConversationSnapshot,
  ) => readonly ChatMessage[] | undefined;
  /** Artificial resolve delay in milliseconds for load and send. Default 0. */
  readonly latencyMs?: number;
  /** When true, sendMessage rejects so failure handling can be exercised. */
  readonly failSend?: boolean;
}

function delay(ms: number): Promise<void> {
  if (ms <= 0) return Promise.resolve();
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class InMemoryChatProvider implements ChatConversationProvider {
  private readonly conversationId: string;
  private title: string | undefined;
  private status: ChatConversationStatus;
  private agentLiveness: ChatAgentLiveness;
  private messages: ChatMessage[];
  private readonly autoReply: InMemoryChatSeed["autoReply"];
  private readonly latencyMs: number;
  private failSend: boolean;
  private readonly listeners = new Map<string, Set<ChatInvalidationListener>>();
  private sequence = 0;

  constructor(seed: InMemoryChatSeed) {
    this.conversationId = seed.conversationId;
    this.title = seed.title;
    this.status = seed.status ?? "open";
    this.agentLiveness = seed.agentLiveness ?? "unknown";
    this.messages = seed.messages !== undefined ? [...seed.messages] : [];
    this.autoReply = seed.autoReply;
    this.latencyMs = seed.latencyMs ?? 0;
    this.failSend = seed.failSend ?? false;
  }

  /** Stable id generator for fixture-authored messages. */
  nextId(prefix = "msg"): string {
    this.sequence += 1;
    return `${prefix}-${this.sequence}`;
  }

  private snapshot(): ChatConversationSnapshot {
    return {
      conversationId: this.conversationId,
      title: this.title,
      status: this.status,
      agentLiveness: this.agentLiveness,
      messages: [...this.messages],
    };
  }

  private notify(conversationId: string): void {
    const set = this.listeners.get(conversationId);
    if (set === undefined) return;
    for (const listener of set) listener();
  }

  async loadConversation(
    conversationId: string,
  ): Promise<ChatConversationSnapshot> {
    await delay(this.latencyMs);
    if (conversationId !== this.conversationId) {
      throw new Error(`Unknown conversation ${conversationId}`);
    }
    return this.snapshot();
  }

  async sendMessage(
    conversationId: string,
    input: ChatSendInput,
  ): Promise<ChatConversationSnapshot> {
    await delay(this.latencyMs);
    if (conversationId !== this.conversationId) {
      throw new Error(`Unknown conversation ${conversationId}`);
    }
    if (this.failSend) {
      throw new Error("Message could not be delivered");
    }
    this.messages.push({
      id: this.nextId("user"),
      author: "user",
      content: input.value,
      createdAt: new Date().toISOString(),
    });
    const replies = this.autoReply?.(input, this.snapshot());
    if (replies !== undefined) this.messages.push(...replies);
    this.notify(conversationId);
    return this.snapshot();
  }

  subscribe(
    conversationId: string,
    onInvalidate: ChatInvalidationListener,
  ): ChatUnsubscribe {
    let set = this.listeners.get(conversationId);
    if (set === undefined) {
      set = new Set();
      this.listeners.set(conversationId, set);
    }
    set.add(onInvalidate);
    return () => {
      set?.delete(onInvalidate);
    };
  }

  // --- Fixture controls (test and harness only) ---------------------------

  /** Append a message from any author and notify subscribers. */
  pushMessage(message: ChatMessage): void {
    this.messages.push(message);
    this.notify(this.conversationId);
  }

  /** Flip the driving-agent liveness and notify subscribers. */
  setAgentLiveness(liveness: ChatAgentLiveness): void {
    this.agentLiveness = liveness;
    this.notify(this.conversationId);
  }

  /** Flip the conversation status and notify subscribers. */
  setStatus(status: ChatConversationStatus): void {
    this.status = status;
    this.notify(this.conversationId);
  }

  /** Toggle the send-failure switch at runtime. */
  setFailSend(failSend: boolean): void {
    this.failSend = failSend;
  }
}
