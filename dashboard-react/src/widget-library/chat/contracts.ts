// Typed chat primitives for the Co-work Chat tab and future conversational
// surfaces. These types mirror the house conversation_* semantics (the legacy
// dashboard chat-sidebar seam) in a JSON-compatible, transport-agnostic shape.
// No HTTP wiring lives here. The provider seam is the contract a live
// transport implements.

/**
 * Canonical author of a rendered message. The house backend labels an agent
 * turn "agent" and a human turn "user". This surface maps "agent" onto
 * "assistant" and reserves "system" for non-attributed notices.
 */
export type ChatAuthorRole = "user" | "assistant" | "system";

/** How the human is expected to answer a pending question. */
export type ChatResponseType = "freeform" | "boolean" | "choice";

/** One labelled option for a choice question. */
export interface ChatChoice {
  readonly key: string;
  readonly label: string;
}

/**
 * The answerable shape attached to a pending question message. Boolean and
 * choice questions render inline affordances, freeform falls back to the
 * ordinary composer.
 */
export interface ChatQuestion {
  readonly responseType: ChatResponseType;
  readonly choices?: readonly ChatChoice[];
}

/** One conversation message as displayed in the transcript. */
export interface ChatMessage {
  readonly id: string;
  readonly author: ChatAuthorRole;
  /** Optional display name override, e.g. a named assistant persona. */
  readonly authorLabel?: string;
  readonly content: string;
  /** ISO-8601 timestamp. Absent messages render without a time stamp. */
  readonly createdAt?: string;
  /** True when this message is a question still awaiting the human answer. */
  readonly pending?: boolean;
  /** Present when the message is a structured question, drives inline answers. */
  readonly question?: ChatQuestion;
}

/** Whether the conversation still accepts input. */
export type ChatConversationStatus = "open" | "closed";

/**
 * Liveness of the driving agent process, mirroring conversation.agent_alive.
 * "alive" == true, "stopped" == false, "unknown" == null (no registered driver).
 */
export type ChatAgentLiveness = "alive" | "stopped" | "unknown";

/** A full point-in-time view of one conversation. */
export interface ChatConversationSnapshot {
  readonly conversationId: string;
  readonly title?: string;
  readonly status: ChatConversationStatus;
  readonly agentLiveness: ChatAgentLiveness;
  readonly messages: readonly ChatMessage[];
}

/**
 * A human-authored outbound value. For a freeform reply this is the typed text,
 * for a boolean question "true" or "false", for a choice question the choice
 * key. inReplyTo optionally names the pending question being answered.
 */
export interface ChatSendInput {
  readonly value: string;
  readonly inReplyTo?: string;
}

/** Called by a provider when its view of a conversation may have changed. */
export type ChatInvalidationListener = () => void;

/** Tear down a subscription registered through the provider. */
export type ChatUnsubscribe = () => void;

/**
 * The provider seam. A live implementation maps loadConversation onto
 * GET /api/conversations/<id>, sendMessage onto POST .../respond, and subscribe
 * onto the 3s poll loop or an SSE-driven invalidation. This module ships only
 * the interface plus an in-memory fixture. It never performs I/O itself.
 */
export interface ChatConversationProvider {
  /** Load the current snapshot for a conversation. */
  loadConversation(conversationId: string): Promise<ChatConversationSnapshot>;
  /** Submit one human message or answer, resolving with the next snapshot. */
  sendMessage(
    conversationId: string,
    input: ChatSendInput,
  ): Promise<ChatConversationSnapshot>;
  /**
   * Register an invalidation listener. The returned unsubscribe stops delivery.
   * This is the poll or subscribe hook shape, the consumer reloads on notify.
   */
  subscribe(
    conversationId: string,
    onInvalidate: ChatInvalidationListener,
  ): ChatUnsubscribe;
}

/**
 * Derived activity signal for the transcript. "thinking" shows the typing
 * indicator, "stopped" shows the agent-stopped notice, "idle" shows neither.
 */
export type ChatAgentActivity = "thinking" | "stopped" | "idle";

/**
 * Host presentation state for the whole panel, mirroring the dashboard
 * host-state contract (SnapshotStatus plus loading/empty). "ready" and
 * "read-only" render the transcript, the rest are full-panel placeholders.
 */
export type ChatPanelStatus =
  | "ready"
  | "loading"
  | "empty"
  | "error"
  | "read-only";

// --- Raw house-conversation payload shapes (the mirroring source) ---------
// These describe the JSON that GET /api/conversations/<id> returns today, so a
// live transport can normalize into the canonical types above with the pure
// helper in mapping.ts. They are documentation of the seam, not a fetch layer.

export interface RawChatChoice {
  readonly key: string;
  readonly label: string;
}

export interface RawChatMessage {
  /** The endpoint's message identity field (ConversationMessage.to_dict). */
  readonly message_id?: string;
  /** Fixture-side fallback identity. The live endpoint never emits this. */
  readonly id?: string | number;
  readonly role?: string;
  readonly content?: string;
  readonly created_at?: string;
  readonly message_type?: string;
  readonly status?: string;
  readonly response_type?: string;
  readonly choices?: readonly RawChatChoice[];
}

export interface RawChatConversation {
  readonly conversation_id: string;
  readonly title?: string;
  readonly status?: string;
  readonly agent_alive?: boolean | null;
}

export interface RawChatConversationPayload {
  readonly conversation: RawChatConversation;
  readonly messages?: readonly RawChatMessage[];
}
