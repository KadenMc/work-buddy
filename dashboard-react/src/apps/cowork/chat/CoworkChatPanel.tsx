// The Co-work Chat tab surface. It binds the live document-conversation
// transport (or any ChatConversationProvider) through the house
// useChatConversation hook, overlays the document linkage from the annotations
// store, and renders the transcript with the feedback span-link affordance and
// routing-note delivery status. It is the richer alternative to the plain house
// ChatPanel: the surface mounts it in place of the demo chat, and the scroll-to
// seam arrives as a callback prop so this module never imports the editor.

import { useEffect, useMemo, useSyncExternalStore } from "react";

import { Button, InlineAlert } from "../../../ui";
import {
  ChatComposer,
  deriveAgentActivity,
  useChatConversation,
  type ChatConversationProvider,
  type ChatMessage,
} from "../../../widget-library/chat";
import { CoworkChatAnnotations } from "./annotations";
import { resolveSpanLinks } from "./annotations";
import { CoworkChatTranscript } from "./CoworkChatTranscript";
import type { ScrollAnchorTarget } from "./contracts";
import type { CoworkDocumentAgent } from "./documentConversationBinding";
import "./styles.css";

export interface CoworkChatPanelProps {
  /** The conversation transport, the live HttpChatConversationProvider in v1. */
  readonly provider: ChatConversationProvider;
  readonly conversationId: string;
  /**
   * The document linkage store. The feedback entry point and the submit path
   * write to it. When omitted an empty store is created, so the panel renders a
   * plain transcript with no span links or routing notices.
   */
  readonly annotations?: CoworkChatAnnotations;
  /** The scroll-to-passage seam, wired by the surface. Not an editor import. */
  readonly onScrollToAnchor?: (target: ScrollAnchorTarget) => void;
  readonly title?: string;
  readonly composerPlaceholder?: string;
  readonly noMessagesLabel?: string;
  /** Seed the composer once, e.g. from a retained unsent draft (route guard). */
  readonly composerInitialValue?: string;
  /** Observe the live composer draft, empty after a successful send. */
  readonly onComposerDraftChange?: (value: string) => void;
  /** Server-owned state of the agent bound to this conversation. */
  readonly agent?: CoworkDocumentAgent;
  /** A restart is in flight; the transcript stays visible while it completes. */
  readonly ensuringAgent?: boolean;
  /** A failed restart, shown without unmounting the existing transcript. */
  readonly ensureError?: string | null;
  /** Present-user-intent restart/start action for a stopped or unavailable agent. */
  readonly onEnsureAgent?: () => void;
  /** Let the owning rail derive unread state without mounting a second chat hook. */
  readonly onMessagesChange?: (messages: readonly ChatMessage[]) => void;
}

const EMPTY_MESSAGES: readonly ChatMessage[] = [];

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
  const chat = useChatConversation(provider, conversationId);
  const store = useMemo(
    () => annotations ?? new CoworkChatAnnotations(),
    [annotations],
  );
  const linkage = useSyncExternalStore(store.subscribe, store.getSnapshot);

  const messages = chat.snapshot?.messages ?? EMPTY_MESSAGES;
  useEffect(() => onMessagesChange?.(messages), [messages, onMessagesChange]);
  const spanLinks = useMemo(
    () => resolveSpanLinks(messages, linkage.feedback),
    [messages, linkage.feedback],
  );
  const agentActivity =
    chat.snapshot !== null ? deriveAgentActivity(chat.snapshot) : "idle";
  const closed = chat.snapshot?.status === "closed";
  const agentStartFailed = agent?.status === "spawn_failed";
  const agentNotStarted = agent?.status === "not_started";
  const agentStopped =
    agent?.status === "stopped" ||
    (!agentStartFailed &&
      !agentNotStarted &&
      agentActivity === "stopped");
  const agentNeedsRecovery =
    agentStartFailed || agentStopped || agentNotStarted;
  const recoveryTitle = ensuringAgent
    ? agentStartFailed
      ? "Trying again…"
      : agentNotStarted
        ? "Starting chat…"
        : "Restarting chat…"
    : agentStartFailed
      ? "Chat couldn’t start."
      : agentNotStarted
        ? "Chat isn’t started."
        : "Chat paused.";
  const rawRecoveryError = ensureError ?? agent?.error;
  const recoveryError =
    rawRecoveryError === "Chat couldn’t start. Try again."
      ? null
      : rawRecoveryError;
  const recoveryDetail = ensuringAgent
    ? "Your messages are still here."
    : recoveryError ??
      (agentStartFailed
        ? "Try again when you’re ready."
        : agentNotStarted
          ? "Start when you’re ready."
          : "Your messages are still here. Restart chat to keep going.");
  const recoveryAction = ensuringAgent
    ? agentStartFailed
      ? "Trying again…"
      : agentNotStarted
        ? "Starting…"
        : "Restarting…"
    : agentStartFailed
      ? "Try again"
      : agentNotStarted
        ? "Start chat"
        : "Restart chat";

  const renderBody = () => {
    if (chat.status === "loading") {
      return (
        <div className="wb-chat-state" role="status">
          <span className="wb-spinner" aria-hidden="true" />
          <h3 className="wb-chat-state__title">Loading chat</h3>
          <p>Checking for earlier messages.</p>
        </div>
      );
    }
    if (chat.status === "error") {
      return (
        <div className="wb-chat-state" role="alert">
          <h3 className="wb-chat-state__title">Chat could not load</h3>
          <p>{chat.error ?? "Try again to load chat."}</p>
          <Button
            variant="secondary"
            className="wb-chat-state__action"
            onClick={chat.retry}
          >
            Retry
          </Button>
        </div>
      );
    }

    return (
      <>
        <CoworkChatTranscript
          messages={messages}
          label={title}
          agentActivity={agentActivity}
          showStoppedNotice={false}
          spanLinks={spanLinks}
          routing={linkage.routing}
          onScrollToAnchor={onScrollToAnchor}
          onRespond={(value, inReplyTo) => {
            // A failed inline answer surfaces through the composer error the
            // hook records before rethrowing. The catch prevents an unhandled
            // rejection on this path.
            void Promise.resolve(chat.send(value, inReplyTo)).catch(() => {});
          }}
          onDismissRouting={(id) => store.dismissRoutingDelivery(id)}
          emptyLabel={noMessagesLabel}
        />
        {closed ? (
          <InlineAlert
            tone="info"
            role="status"
            className="wb-chat-panel__read-only"
          >
            <strong>Read-only:</strong> This conversation is closed.
          </InlineAlert>
        ) : agentNeedsRecovery ? (
          <InlineAlert
            tone={agentStopped || agentStartFailed ? "warning" : "info"}
            role="status"
            className="wb-chat-panel__read-only"
          >
            <strong>{recoveryTitle}</strong>{" "}
            {recoveryDetail}
            {onEnsureAgent !== undefined ? (
              <Button
                variant="secondary"
                className="wb-chat-state__action"
                onClick={onEnsureAgent}
                disabled={ensuringAgent}
              >
                {recoveryAction}
              </Button>
            ) : null}
          </InlineAlert>
        ) : (
          <ChatComposer
            onSend={(value) => chat.send(value)}
            sending={chat.sending}
            placeholder={composerPlaceholder}
            errorMessage={chat.sendError ?? undefined}
            initialValue={composerInitialValue}
            onDraftChange={onComposerDraftChange}
          />
        )}
      </>
    );
  };

  return (
    <section className="wb-chat-panel" aria-label={title}>
      <header className="wb-chat-panel__header">
        <h2 className="wb-chat-panel__title">{title}</h2>
      </header>
      {renderBody()}
    </section>
  );
}

export default CoworkChatPanel;
