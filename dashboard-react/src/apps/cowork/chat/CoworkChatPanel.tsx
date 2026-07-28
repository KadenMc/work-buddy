// The thin Co-work adapter around the dashboard's reusable ConversationChat.
// It maps document-specific feedback, routing, and agent lifecycle state into
// the shared surface's narrow extension seams. Message structure, unread state,
// loading, retries, questions, and composer behavior remain shared.

import {
  useCallback,
  useMemo,
  useSyncExternalStore,
} from "react";

import {
  ConversationChat,
  type ChatConversationProvider,
  type ChatInputRecovery,
  type ChatMessage,
  type ConversationChatState,
} from "../../../widget-library/chat";
import {
  CoworkChatAnnotations,
  resolveSpanLink,
} from "./annotations";
import {
  CoworkPassageAction,
  CoworkRoutingNotices,
} from "./CoworkChatExtensions";
import type { ScrollAnchorTarget } from "./contracts";
import type { CoworkDocumentAgent } from "./documentConversationBinding";
import "./styles.css";

export interface CoworkChatPanelProps {
  /** The reusable house-conversation transport. */
  readonly provider: ChatConversationProvider;
  readonly conversationId: string;
  /**
   * The document linkage store. Feedback and sitting routing write to it. When
   * omitted the shared surface renders with no Co-work message accessories.
   */
  readonly annotations?: CoworkChatAnnotations;
  /** The passage seam, wired by the workspace without importing editor code. */
  readonly onScrollToAnchor?: (target: ScrollAnchorTarget) => void;
  readonly title?: string;
  readonly composerPlaceholder?: string;
  readonly noMessagesLabel?: string;
  /** Seed the conversation-scoped composer from the retained document draft. */
  readonly composerInitialValue?: string;
  /** Observe the live draft, including the empty value after acknowledged send. */
  readonly onComposerDraftChange?: (value: string) => void;
  /** Server-owned state of the document agent bound to this conversation. */
  readonly agent?: CoworkDocumentAgent;
  /** A start or restart request is in flight. */
  readonly ensuringAgent?: boolean;
  /** A failed start or restart request. */
  readonly ensureError?: string | null;
  /** Present-user-intent start or restart action. */
  readonly onEnsureAgent?: () => void;
  /** Let the owning rail derive unread state without mounting another hook. */
  readonly onMessagesChange?: (messages: readonly ChatMessage[]) => void;
}

export function CoworkChatPanel({
  provider,
  conversationId,
  annotations,
  onScrollToAnchor,
  title = "Chat about this document",
  composerPlaceholder,
  noMessagesLabel = "No messages yet. Ask anything about this document.",
  composerInitialValue,
  onComposerDraftChange,
  agent,
  ensuringAgent = false,
  ensureError,
  onEnsureAgent,
  onMessagesChange,
}: CoworkChatPanelProps) {
  const store = useMemo(
    () => annotations ?? new CoworkChatAnnotations(),
    [annotations],
  );
  const linkage = useSyncExternalStore(store.subscribe, store.getSnapshot);

  const renderMessageAccessory = useCallback(
    (message: ChatMessage) => {
      if (onScrollToAnchor === undefined) return null;
      const link = resolveSpanLink(message, linkage.feedback);
      if (link === null) return null;
      return (
        <CoworkPassageAction
          link={link}
          onActivate={onScrollToAnchor}
        />
      );
    },
    [linkage.feedback, onScrollToAnchor],
  );

  const resolveInputRecovery = useCallback(
    (state: ConversationChatState): ChatInputRecovery | undefined => {
      const startFailed = agent?.status === "spawn_failed";
      const notStarted = agent?.status === "not_started";
      const stopped =
        agent?.status === "stopped" ||
        (!startFailed &&
          !notStarted &&
          state.agentActivity === "stopped");
      if (!startFailed && !notStarted && !stopped) return undefined;

      const titleText = ensuringAgent
        ? startFailed
          ? "Trying again…"
          : notStarted
            ? "Starting chat…"
            : "Restarting chat…"
        : startFailed
          ? "Chat couldn’t start."
          : notStarted
            ? "Chat hasn’t started."
            : "Chat paused.";
      const rawError = ensureError ?? agent?.error;
      const detail =
        ensuringAgent
          ? "Your messages are still here."
          : rawError === "Chat couldn’t start. Try again."
            ? "Try again."
            : rawError ??
              (startFailed
                ? "Try again."
                : notStarted
                  ? "Start chat to ask about this document."
                  : "Your messages are still here.");
      const actionLabel = ensuringAgent
        ? startFailed
          ? "Trying again…"
          : notStarted
            ? "Starting…"
            : "Restarting…"
        : startFailed
          ? "Try again"
          : notStarted
            ? "Start chat"
            : "Restart chat";

      return {
        tone: stopped || startFailed ? "warning" : "info",
        title: titleText,
        detail,
        ...(onEnsureAgent === undefined
          ? {}
          : {
              action: {
                label: actionLabel,
                onAction: onEnsureAgent,
                pending: ensuringAgent,
              },
            }),
      };
    },
    [
      agent?.error,
      agent?.status,
      ensureError,
      ensuringAgent,
      onEnsureAgent,
    ],
  );

  return (
    <ConversationChat
      provider={provider}
      conversationId={conversationId}
      title={title}
      composerPlaceholder={composerPlaceholder}
      noMessagesLabel={noMessagesLabel}
      initialValue={composerInitialValue}
      onDraftChange={onComposerDraftChange}
      onMessagesChange={onMessagesChange}
      renderMessageAccessory={renderMessageAccessory}
      transcriptAppendix={
        <CoworkRoutingNotices
          deliveries={linkage.routing}
          onDismiss={(id) => store.dismissRoutingDelivery(id)}
        />
      }
      inputRecovery={resolveInputRecovery}
      readOnlyReason="This chat is closed."
    />
  );
}

export default CoworkChatPanel;
