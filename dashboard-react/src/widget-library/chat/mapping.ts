// Pure, transport-free helpers that translate the raw house-conversation
// payload into the canonical chat types and derive display signals. A live
// transport reuses normalizeConversationPayload so the mirroring of
// conversation_* semantics lives in one tested place.

import type {
  ChatActionSnapshotContext,
  ChatAgentActivity,
  ChatAgentLiveness,
  ChatAuthorRole,
  ChatConversationSnapshot,
  ChatConversationStatus,
  ChatMessage,
  ChatMessageProducer,
  ChatQuestion,
  ChatResponseType,
  RawChatConversationPayload,
  RawChatMessage,
} from "./contracts";

const RESPONSE_TYPES: ReadonlySet<string> = new Set([
  "freeform",
  "boolean",
  "choice",
]);

/** Map the backend role token onto a canonical author. "agent" becomes assistant. */
export function toAuthorRole(role: string | undefined): ChatAuthorRole {
  if (role === "user") return "user";
  if (role === "system") return "system";
  return "assistant";
}

/** Map conversation.agent_alive (true/false/null) onto the liveness enum. */
export function toAgentLiveness(
  agentAlive: boolean | null | undefined,
): ChatAgentLiveness {
  if (agentAlive === true) return "alive";
  if (agentAlive === false) return "stopped";
  return "unknown";
}

function toResponseType(raw: string | undefined): ChatResponseType {
  if (raw !== undefined && RESPONSE_TYPES.has(raw)) {
    return raw as ChatResponseType;
  }
  return "freeform";
}

function optionalProducer(raw: RawChatMessage): ChatMessageProducer | undefined {
  const producer = raw.producer;
  if (producer === undefined || producer === null) return undefined;
  const providerId =
    typeof producer.provider_id === "string" && producer.provider_id.length > 0
      ? producer.provider_id
      : undefined;
  const modelId =
    typeof producer.model_id === "string" && producer.model_id.length > 0
      ? producer.model_id
      : undefined;
  const providerLabel =
    typeof producer.provider_label === "string" &&
    producer.provider_label.length > 0
      ? producer.provider_label
      : providerId;
  const modelLabel =
    typeof producer.model_label === "string" && producer.model_label.length > 0
      ? producer.model_label
      : modelId;
  if (providerLabel === undefined || modelLabel === undefined) return undefined;
  return {
    providerId,
    modelId,
    providerLabel,
    modelLabel,
  };
}

function optionalActionContext(
  raw: RawChatMessage,
): ChatActionSnapshotContext | undefined {
  const context = raw.context;
  if (
    context?.kind !== "action_snapshot" ||
    typeof context.action_snapshot_id !== "string" ||
    typeof context.store_id !== "string" ||
    typeof context.document_id !== "string" ||
    (context.target_kind !== "document" &&
      context.target_kind !== "text_quote") ||
    typeof context.target_label !== "string" ||
    typeof context.target_text_sha256 !== "string" ||
    typeof context.projection_sha256 !== "string" ||
    typeof context.captured_at !== "string"
  ) {
    return undefined;
  }
  const consumption =
    typeof context.consumption === "object" &&
    context.consumption !== null &&
    "receipt_id" in context.consumption &&
    typeof context.consumption.receipt_id === "string" &&
    "user_message_id" in context.consumption &&
    typeof context.consumption.user_message_id === "string" &&
    "fetched_at" in context.consumption &&
    typeof context.consumption.fetched_at === "string"
      ? {
          receiptId: context.consumption.receipt_id,
          userMessageId: context.consumption.user_message_id,
          fetchedAt: context.consumption.fetched_at,
          fetchOutcome:
            "fetch_outcome" in context.consumption &&
            context.consumption.fetch_outcome === "unavailable"
              ? ("unavailable" as const)
              : ("available" as const),
          unavailableCode:
            "unavailable_code" in context.consumption &&
            typeof context.consumption.unavailable_code === "string"
              ? context.consumption.unavailable_code
              : undefined,
        }
      : undefined;
  const discussion =
    typeof context.discussion === "object" &&
    context.discussion !== null &&
    "kind" in context.discussion &&
    context.discussion.kind === "cothink_item" &&
    "item_id" in context.discussion &&
    typeof context.discussion.item_id === "string" &&
    "canonical_sha256" in context.discussion &&
    typeof context.discussion.canonical_sha256 === "string" &&
    "content" in context.discussion &&
    typeof context.discussion.content === "string" &&
    "rationale" in context.discussion &&
    typeof context.discussion.rationale === "string" &&
    "non_evidential" in context.discussion &&
    context.discussion.non_evidential === true
      ? {
          kind: "cothink_item" as const,
          itemId: context.discussion.item_id,
          canonicalSha256: context.discussion.canonical_sha256,
          content: context.discussion.content,
          rationale: context.discussion.rationale,
          nonEvidential: true as const,
        }
      : undefined;
  return {
    kind: "action_snapshot",
    actionSnapshotId: context.action_snapshot_id,
    storeId: context.store_id,
    documentId: context.document_id,
    targetKind: context.target_kind,
    targetLabel: context.target_label,
    targetWordCount:
      typeof context.target_word_count === "number"
        ? context.target_word_count
        : undefined,
    targetTextSha256: context.target_text_sha256,
    projectionSha256: context.projection_sha256,
    capturedAt: context.captured_at,
    consumption,
    discussion,
  };
}

function toMessage(raw: RawChatMessage, index: number): ChatMessage {
  const pending = raw.status === "pending" && raw.message_type === "question";
  let question: ChatQuestion | undefined;
  if (raw.message_type === "question") {
    const responseType = toResponseType(raw.response_type);
    question = {
      responseType,
      choices:
        responseType === "choice" && raw.choices !== undefined
          ? raw.choices.map((choice) => ({
              key: choice.key,
              label: choice.label,
            }))
          : undefined,
    };
  }
  // The live endpoint serializes the identity field as message_id
  // (ConversationMessage.to_dict). A bare id is accepted as a fixture-side
  // fallback only, and the positional id is the last resort.
  const rawId = raw.message_id ?? raw.id;
  return {
    id: rawId !== undefined ? String(rawId) : `msg-${index}`,
    author: toAuthorRole(raw.role),
    content: raw.content ?? "",
    createdAt: raw.created_at,
    pending,
    question,
    producer: optionalProducer(raw),
    context: optionalActionContext(raw),
  };
}

/**
 * Normalize the raw GET /api/conversations/<id> payload into a snapshot. Pure,
 * total, and defensive against missing optional fields.
 */
export function normalizeConversationPayload(
  payload: RawChatConversationPayload,
): ChatConversationSnapshot {
  const status: ChatConversationStatus =
    payload.conversation.status === "closed" ? "closed" : "open";
  return {
    conversationId: payload.conversation.conversation_id,
    title: payload.conversation.title,
    status,
    agentLiveness: toAgentLiveness(payload.conversation.agent_alive),
    messages: (payload.messages ?? []).map(toMessage),
  };
}

/**
 * Derive the transcript activity signal from a snapshot, mirroring the legacy
 * _computeAgentTyping and agent-dead logic:
 *  - stopped: conversation open and the driver process exited
 *  - thinking: open, no pending question, and the agent still appears to be
 *    working (last turn is the human, or the agent is mid-stream sending
 *    text rather than a question)
 *  - idle: everything else
 */
export function deriveAgentActivity(
  snapshot: ChatConversationSnapshot,
): ChatAgentActivity {
  if (snapshot.status !== "open") return "idle";
  if (snapshot.agentLiveness === "stopped") return "stopped";

  const messages = snapshot.messages;
  const hasPending = messages.some((message) => message.pending === true);
  if (hasPending) return "idle";

  const last = messages.length > 0 ? messages[messages.length - 1] : undefined;
  if (last === undefined) return "idle";

  // The agent has explicitly handed control back with a question.
  if (last.author !== "user" && last.question !== undefined) return "idle";

  // A live driver is mid-flow. With no registered driver (unknown) fall back to
  // showing activity only while the human holds the last turn.
  if (snapshot.agentLiveness === "alive") return "thinking";
  return last.author === "user" ? "thinking" : "idle";
}
