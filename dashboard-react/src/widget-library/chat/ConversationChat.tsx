import { useEffect, useMemo } from "react";

import {
  ChatPanel,
  type ChatInputRecovery,
  type ChatPanelProps,
} from "./ChatPanel";
import type {
  ChatAgentActivity,
  ChatConversationProvider,
  ChatConversationSnapshot,
  ChatMessage,
  ChatPanelStatus,
  ChatSendInput,
} from "./contracts";
import {
  useChatConversation,
  type ChatLoadStatus,
} from "./useChatConversation";
import { deriveAgentActivity } from "./mapping";

const EMPTY_MESSAGES: readonly ChatMessage[] = [];

/**
 * Transport-derived state exposed to a feature-owned recovery resolver. The
 * resolver may choose presentation only; the shared surface retains ownership
 * of loading, sending, retry, transcript, and composer behavior.
 */
export interface ConversationChatState {
  readonly conversationId: string;
  readonly snapshot: ChatConversationSnapshot | null;
  readonly loadStatus: ChatLoadStatus;
  readonly loadError: string | null;
  readonly sending: boolean;
  readonly sendError: string | null;
  readonly agentActivity: ChatAgentActivity;
}

export type ChatInputRecoveryResolver = (
  state: ConversationChatState,
) => ChatInputRecovery | undefined;

/** Additive pre-send seam for hosts that explicitly attach durable context. */
export type ChatSendPreparer = (
  input: ChatSendInput,
) => ChatSendInput | Promise<ChatSendInput>;

type SharedConversationPanelProps = Omit<
  ChatPanelProps,
  | "status"
  | "messages"
  | "agentActivity"
  | "onSend"
  | "sending"
  | "sendErrorMessage"
  | "errorMessage"
  | "onRetry"
  | "inputRecovery"
>;

export type ConversationChatProps = SharedConversationPanelProps & {
  readonly provider: ChatConversationProvider;
  readonly conversationId: string;
  /**
   * A fixed recovery descriptor or a pure resolver over the current
   * transport-derived state.
   */
  readonly inputRecovery?: ChatInputRecovery | ChatInputRecoveryResolver;
  /** Observe the canonical message list without mounting another chat hook. */
  readonly onMessagesChange?: (messages: readonly ChatMessage[]) => void;
  /** Prepare an outbound turn before the provider sees it. */
  readonly prepareSend?: ChatSendPreparer;
};

function panelStatus(
  loadStatus: ChatLoadStatus,
  snapshot: ChatConversationSnapshot | null,
): ChatPanelStatus {
  if (loadStatus === "loading") return "loading";
  if (loadStatus === "error") return "error";
  return snapshot?.status === "closed" ? "read-only" : "ready";
}

/**
 * Reusable provider-driven conversation surface. Feature code supplies only a
 * stable provider, an opaque conversation id, and optional additive
 * presentation extensions.
 */
export function ConversationChat({
  provider,
  conversationId,
  inputRecovery,
  onMessagesChange,
  prepareSend,
  readOnlyReason = "This conversation is closed.",
  ...panelProps
}: ConversationChatProps) {
  const chat = useChatConversation(provider, conversationId);
  const messages = chat.snapshot?.messages ?? EMPTY_MESSAGES;
  const activity =
    chat.snapshot === null ? "idle" : deriveAgentActivity(chat.snapshot);
  const state = useMemo<ConversationChatState>(
    () => ({
      conversationId,
      snapshot: chat.snapshot,
      loadStatus: chat.status,
      loadError: chat.error,
      sending: chat.sending,
      sendError: chat.sendError,
      agentActivity: activity,
    }),
    [
      activity,
      chat.error,
      chat.sendError,
      chat.sending,
      chat.snapshot,
      chat.status,
      conversationId,
    ],
  );
  const resolvedRecovery =
    typeof inputRecovery === "function"
      ? inputRecovery(state)
      : inputRecovery;

  useEffect(() => {
    onMessagesChange?.(messages);
  }, [messages, onMessagesChange]);

  return (
    <ChatPanel
      {...panelProps}
      // ChatPanel and its internally-owned composer/list state must not leak
      // across opaque conversation identities.
      key={conversationId}
      status={panelStatus(chat.status, chat.snapshot)}
      messages={messages}
      agentActivity={activity}
      onSend={async (value, inReplyTo) => {
        const input: ChatSendInput = { value, inReplyTo };
        const prepared =
          prepareSend === undefined ? input : await prepareSend(input);
        await chat.send(
          prepared.value,
          prepared.inReplyTo,
          prepared.context,
        );
      }}
      sending={chat.sending}
      sendErrorMessage={chat.sendError ?? undefined}
      errorMessage={chat.error ?? undefined}
      onRetry={chat.retry}
      inputRecovery={resolvedRecovery}
      readOnlyReason={readOnlyReason}
    />
  );
}

export default ConversationChat;
