// The Co-work Chat tab surface. It binds the live document-conversation
// transport (or any ChatConversationProvider) through the house
// useChatConversation hook, overlays the document linkage from the annotations
// store, and renders the transcript with the feedback span-link affordance and
// routing-note delivery status. It is the richer alternative to the plain house
// ChatPanel: the surface mounts it in place of the demo chat, and the scroll-to
// seam arrives as a callback prop so this module never imports the editor.

import { useMemo, useSyncExternalStore } from "react";

import { Button, InlineAlert } from "../../../ui";
import {
  ChatComposer,
  deriveAgentActivity,
  useChatConversation,
  type ChatConversationProvider,
} from "../../../widget-library/chat";
import { CoworkChatAnnotations } from "./annotations";
import { resolveSpanLinks } from "./annotations";
import { CoworkChatTranscript } from "./CoworkChatTranscript";
import type { ScrollAnchorTarget } from "./contracts";
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
}

export function CoworkChatPanel({
  provider,
  conversationId,
  annotations,
  onScrollToAnchor,
  title = "Document conversation",
  composerPlaceholder,
  noMessagesLabel = "No messages yet. Ask the document agent anything.",
  composerInitialValue,
  onComposerDraftChange,
}: CoworkChatPanelProps) {
  const chat = useChatConversation(provider, conversationId);
  const store = useMemo(
    () => annotations ?? new CoworkChatAnnotations(),
    [annotations],
  );
  const linkage = useSyncExternalStore(store.subscribe, store.getSnapshot);

  const messages = chat.snapshot?.messages ?? [];
  const spanLinks = useMemo(
    () => resolveSpanLinks(messages, linkage.feedback),
    [messages, linkage.feedback],
  );
  const agentActivity =
    chat.snapshot !== null ? deriveAgentActivity(chat.snapshot) : "idle";
  const closed = chat.snapshot?.status === "closed";

  const renderBody = () => {
    if (chat.status === "loading") {
      return (
        <div className="wb-chat-state" role="status">
          <span className="wb-spinner" aria-hidden="true" />
          <h3 className="wb-chat-state__title">Loading conversation</h3>
          <p>Work Buddy is preparing this conversation.</p>
        </div>
      );
    }
    if (chat.status === "error") {
      return (
        <div className="wb-chat-state" role="alert">
          <h3 className="wb-chat-state__title">Conversation could not load</h3>
          <p>{chat.error ?? "Try again to reload this conversation."}</p>
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
        ) : (
          <ChatComposer
            onSend={(value) => chat.send(value)}
            sending={chat.sending}
            disabled={agentActivity === "stopped"}
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
