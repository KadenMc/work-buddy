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

let fallbackMessageSequence = 0;

const newUserMessageId = (): string => {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return `chat-user-${globalThis.crypto.randomUUID()}`;
  }
  fallbackMessageSequence += 1;
  return `chat-user-${Date.now().toString(36)}-${fallbackMessageSequence.toString(36)}`;
};

interface PendingSendEnvelope {
  readonly provider: ChatConversationProvider;
  readonly conversationId: string;
  readonly value: string;
  readonly inReplyTo?: string;
  readonly messageId: string;
  readonly sendScopeKey?: string;
  prepared?: ChatSendInput;
}

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
  /** Host authority generation. A change resets only logical-send retry context, never the chat or composer. */
  readonly sendScopeKey?: string;
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
  sendScopeKey,
  readOnlyReason = "This conversation is closed.",
  ...panelProps
}: ConversationChatProps) {
  const chat = useChatConversation(provider, conversationId);
  const activeBinding = useRef({ provider, conversationId, sendScopeKey });
  activeBinding.current = { provider, conversationId, sendScopeKey };
  const pendingSendRef = useRef<PendingSendEnvelope | null>(null);
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
    pendingSendRef.current = null;
  }, [conversationId, provider]);
  useEffect(() => {
    setPrepareError(null);
    pendingSendRef.current = null;
  }, [sendScopeKey]);

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
        let pending = pendingSendRef.current;
        if (
          pending === null ||
          pending.provider !== expectedProvider ||
          pending.conversationId !== expectedConversationId ||
          pending.sendScopeKey !== sendScopeKey ||
          pending.value !== value ||
          pending.inReplyTo !== inReplyTo
        ) {
          pending = {
            provider: expectedProvider,
            conversationId: expectedConversationId,
            value,
            inReplyTo,
            messageId: newUserMessageId(),
            sendScopeKey,
          };
          pendingSendRef.current = pending;
        }
        let prepared: ChatSendInput;
        try {
          if (pending.prepared === undefined) {
            const input: ChatSendInput = {
              value,
              inReplyTo,
              messageId: pending.messageId,
            };
            const candidate =
              prepareSend === undefined ? input : await prepareSend(input);
            if (activeBinding.current.provider !== expectedProvider || activeBinding.current.conversationId !== expectedConversationId || activeBinding.current.sendScopeKey !== sendScopeKey) {
              throw new Error("This message's context changed before it could be sent. Please send it again.");
            }
            // The shared surface owns retry identity. A host may enrich the
            // send but cannot accidentally drop or replace that identity.
            pending.prepared = {
              ...candidate,
              messageId: pending.messageId,
            };
          }
          prepared = pending.prepared;
        } catch (error) {
          if (
            activeBinding.current.provider === expectedProvider &&
            activeBinding.current.conversationId === expectedConversationId &&
            activeBinding.current.sendScopeKey === sendScopeKey
          ) {
            setPrepareError(errorMessage(error));
          }
          throw error;
        }
        await chat.send(
          prepared.value,
          prepared.inReplyTo,
          prepared.context,
          prepared.messageId,
        );
        if (pendingSendRef.current === pending) {
          pendingSendRef.current = null;
        }
        if (
          activeBinding.current.provider === expectedProvider &&
          activeBinding.current.conversationId === expectedConversationId &&
          activeBinding.current.sendScopeKey === sendScopeKey
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
