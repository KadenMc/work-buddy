import { useEffect, useRef, useState } from "react";

import { ChatPanel, type ChatPanelProps } from "./ChatPanel";
import type {
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

const errorMessage = (error: unknown): string =>
  error instanceof Error && error.message.trim().length > 0
    ? error.message
    : "Message context could not be prepared.";

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
>;

export type ConversationChatProps = SharedConversationPanelProps & {
  readonly provider: ChatConversationProvider;
  readonly conversationId: string;
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
  onMessagesChange,
  prepareSend,
  readOnlyReason = "This conversation is closed.",
  ...panelProps
}: ConversationChatProps) {
  const chat = useChatConversation(provider, conversationId);
  const activeBinding = useRef({ provider, conversationId });
  activeBinding.current = { provider, conversationId };
  const [prepareError, setPrepareError] = useState<string | null>(null);
  const [revealLatestMessageToken, setRevealLatestMessageToken] = useState(0);
  const messages = chat.snapshot?.messages ?? EMPTY_MESSAGES;
  const activity =
    chat.snapshot === null ? "idle" : deriveAgentActivity(chat.snapshot);
  useEffect(() => {
    onMessagesChange?.(messages);
  }, [messages, onMessagesChange]);

  useEffect(() => {
    setPrepareError(null);
    setRevealLatestMessageToken(0);
  }, [conversationId, provider]);

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
        const expectedProvider = provider;
        const expectedConversationId = conversationId;
        setPrepareError(null);
        const input: ChatSendInput = { value, inReplyTo };
        let prepared: ChatSendInput;
        try {
          prepared =
            prepareSend === undefined ? input : await prepareSend(input);
        } catch (error) {
          if (
            activeBinding.current.provider === expectedProvider &&
            activeBinding.current.conversationId === expectedConversationId
          ) {
            setPrepareError(errorMessage(error));
          }
          throw error;
        }
        await chat.send(
          prepared.value,
          prepared.inReplyTo,
          prepared.context,
        );
        if (
          activeBinding.current.provider === expectedProvider &&
          activeBinding.current.conversationId === expectedConversationId
        ) {
          setRevealLatestMessageToken((token) => token + 1);
        }
      }}
      sending={chat.sending}
      sendErrorMessage={prepareError ?? chat.sendError ?? undefined}
      errorMessage={chat.error ?? undefined}
      onRetry={chat.retry}
      readOnlyReason={readOnlyReason}
      revealLatestMessageToken={revealLatestMessageToken}
    />
  );
}

export default ConversationChat;
