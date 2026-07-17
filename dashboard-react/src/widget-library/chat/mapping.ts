// Pure, transport-free helpers that translate the raw house-conversation
// payload into the canonical chat types and derive display signals. A live
// transport (join or wave-2 work) reuses normalizeConversationPayload so the
// mirroring of conversation_* semantics lives in one tested place.

import type {
  ChatAgentActivity,
  ChatAgentLiveness,
  ChatAuthorRole,
  ChatConversationSnapshot,
  ChatConversationStatus,
  ChatMessage,
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
  return {
    id: raw.id !== undefined ? String(raw.id) : `msg-${index}`,
    author: toAuthorRole(raw.role),
    content: raw.content ?? "",
    createdAt: raw.created_at,
    pending,
    question,
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
