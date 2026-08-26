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
  /**
   * Server-verified execution provenance for an assistant turn. This is
   * recorded when the message is produced, so a later model switch does not
   * rewrite the history shown beside an earlier reply.
   */
  readonly producer?: ChatMessageProducer;
  /** Explicit, durable context attached to this authored turn. */
  readonly context?: ChatActionSnapshotContext;
}

/** Exact provider/model that produced one durable assistant message. */
export interface ChatMessageProducer {
  readonly providerId?: string;
  readonly modelId?: string;
  readonly providerLabel: string;
  readonly modelLabel: string;
}

/**
 * Immutable document context attached by an explicit targeted-chat gesture.
 * The stable action snapshot ID is the agent delivery and acknowledgement
 * boundary; the remaining fields are transcript-visible provenance.
 */
export interface ChatActionSnapshotContext {
  readonly kind: "action_snapshot";
  readonly actionSnapshotId: string;
  readonly storeId: string;
  readonly documentId: string;
  readonly targetKind: "document" | "text_quote";
  readonly targetLabel: string;
  readonly targetWordCount?: number;
  readonly targetTextSha256: string;
  readonly projectionSha256: string;
  readonly capturedAt: string;
  readonly consumption?: {
    readonly receiptId: string;
    readonly userMessageId: string;
    readonly fetchedAt: string;
    /** Whether this generation could actually open the frozen context. */
    readonly fetchOutcome: "available" | "unavailable";
    readonly unavailableCode?: string;
  };
  readonly discussion?: {
    readonly kind: "cothink_item";
    readonly itemId: string;
    readonly canonicalSha256: string;
    readonly content: string;
    readonly rationale: string;
    readonly nonEvidential: true;
  };
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
  /**
   * Caller-stable identity for this authored turn. Providers must preserve it
   * across transport retries so an uncertain acknowledgement cannot duplicate
   * the durable message.
   */
  readonly messageId?: string;
  /** Omitted for ordinary Chat; present only after explicit context capture. */
  readonly context?: ChatActionSnapshotContext;
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

/** One selectable model advertised by an execution provider. */
export interface ChatExecutionModelOption {
  readonly id: string;
  readonly label: string;
  readonly available: boolean;
  readonly description?: string;
  readonly unavailableReason?: string;
}

/**
 * An authentication and runtime route such as Claude Code or Codex. Direct API
 * transports are deliberately separate provider entries.
 */
export interface ChatExecutionProviderOption {
  readonly id: string;
  readonly label: string;
  readonly available: boolean;
  readonly authMode?: string;
  readonly description?: string;
  readonly unavailableReason?: string;
  readonly models: readonly ChatExecutionModelOption[];
}

/** The server-confirmed atomic provider/model pair for one execution target. */
export interface ChatExecutionSelection {
  readonly providerId: string;
  readonly modelId: string;
  readonly providerLabel: string;
  readonly modelLabel: string;
  readonly revision: string;
}

/** Atomic selection mutation with optimistic-concurrency protection. */
export interface ChatExecutionSelectionInput {
  readonly providerId: string;
  readonly modelId: string;
  readonly expectedRevision: string;
}

/** Point-in-time execution selection and the catalog it was validated against. */
export interface ChatExecutionSnapshot {
  readonly selection: ChatExecutionSelection;
  readonly providers: readonly ChatExecutionProviderOption[];
  /** A read-only target remains inspectable but cannot change its selection. */
  readonly readOnly?: boolean;
}

/**
 * Transport-neutral execution profile seam. It stays separate from the
 * transcript provider so message delivery does not acquire model mechanics.
 */
export interface ChatExecutionProfileProvider {
  load(targetId: string): Promise<ChatExecutionSnapshot>;
  select(
    targetId: string,
    selection: ChatExecutionSelectionInput,
  ): Promise<ChatExecutionSnapshot>;
  /** Drop transport-side discovery caches before the next explicit reload. */
  refresh?(targetId: string): void;
  subscribe(
    targetId: string,
    onInvalidate: ChatInvalidationListener,
  ): ChatUnsubscribe;
}

/**
 * Derived activity signal for the transcript. "starting" shows passive
 * first-turn feedback without locking interaction, "thinking" shows the same
 * feedback while a user turn is being processed, "stopped" shows the
 * agent-stopped notice, and "idle" shows neither.
 */
export type ChatAgentActivity =
  | "starting"
  | "thinking"
  | "stopped"
  | "idle";

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
  readonly producer?: RawChatMessageProducer;
  readonly context?: RawChatActionSnapshotContext;
}

export interface RawChatMessageProducer {
  readonly provider_id?: unknown;
  readonly model_id?: unknown;
  readonly provider_label?: unknown;
  readonly model_label?: unknown;
}

export interface RawChatActionSnapshotContext {
  readonly kind?: unknown;
  readonly action_snapshot_id?: unknown;
  readonly store_id?: unknown;
  readonly document_id?: unknown;
  readonly target_kind?: unknown;
  readonly target_label?: unknown;
  readonly target_word_count?: unknown;
  readonly target_text_sha256?: unknown;
  readonly projection_sha256?: unknown;
  readonly captured_at?: unknown;
  readonly consumption?: unknown;
  readonly discussion?: unknown;
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
