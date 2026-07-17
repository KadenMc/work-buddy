import type { ReactNode } from "react";

import { Button, InlineAlert } from "../../ui";
import { ChatComposer } from "./ChatComposer";
import { ChatMessageList } from "./ChatMessageList";
import type {
  ChatAgentActivity,
  ChatMessage,
  ChatPanelStatus,
} from "./contracts";
import "./styles.css";

export interface ChatPanelProps {
  /** Host presentation state, mirroring the dashboard host-state contract. */
  readonly status?: ChatPanelStatus;
  readonly messages: readonly ChatMessage[];
  /** Accessible name for the panel and the default header text. */
  readonly title?: string;
  /** Header slot. Overrides the default title bar when provided. */
  readonly header?: ReactNode;
  readonly agentActivity?: ChatAgentActivity;
  /** Send intent for freeform messages and inline question answers. */
  onSend?(value: string): void | Promise<void>;
  readonly sending?: boolean;
  readonly sendErrorMessage?: string;
  readonly composerDisabled?: boolean;
  readonly composerPlaceholder?: string;
  /** Reason shown in place of the composer when status is "read-only". */
  readonly readOnlyReason?: string;
  /** Full-panel copy for the "empty" host state. */
  readonly emptyMessage?: string;
  /** Full-panel copy for the "error" host state. */
  readonly errorMessage?: string;
  readonly onRetry?: () => void;
  readonly initialUnreadFromMessageId?: string | null;
  readonly onReachLatest?: () => void;
  /** Empty-transcript copy inside a ready conversation. */
  readonly noMessagesLabel?: string;
}

interface StateCopy {
  readonly title: string;
  readonly message: string;
}

const LOADING_COPY: StateCopy = {
  title: "Loading conversation",
  message: "Work Buddy is preparing this conversation.",
};
const EMPTY_COPY: StateCopy = {
  title: "No conversation",
  message: "There is no conversation to show yet.",
};
const ERROR_COPY: StateCopy = {
  title: "Conversation could not load",
  message: "Try again to reload this conversation.",
};

export function ChatPanel({
  status = "ready",
  messages,
  title,
  header,
  agentActivity = "idle",
  onSend,
  sending = false,
  sendErrorMessage,
  composerDisabled = false,
  composerPlaceholder,
  readOnlyReason,
  emptyMessage,
  errorMessage,
  onRetry,
  initialUnreadFromMessageId,
  onReachLatest,
  noMessagesLabel,
}: ChatPanelProps) {
  const label = title ?? "Conversation";

  const renderHeader = () => {
    if (header !== undefined) {
      return <header className="wb-chat-panel__header">{header}</header>;
    }
    if (title !== undefined) {
      return (
        <header className="wb-chat-panel__header">
          <h2 className="wb-chat-panel__title">{title}</h2>
        </header>
      );
    }
    return null;
  };

  const renderTranscript = () => (
    <ChatMessageList
      messages={messages}
      label={label}
      agentActivity={agentActivity}
      onRespond={
        onSend === undefined
          ? undefined
          : (value) => {
              void onSend(value);
            }
      }
      initialUnreadFromMessageId={initialUnreadFromMessageId}
      onReachLatest={onReachLatest}
      emptyLabel={noMessagesLabel}
    />
  );

  const renderBody = () => {
    if (status === "loading") {
      return (
        <div className="wb-chat-state" role="status">
          <span className="wb-spinner" aria-hidden="true" />
          <h3 className="wb-chat-state__title">{LOADING_COPY.title}</h3>
          <p>{LOADING_COPY.message}</p>
        </div>
      );
    }
    if (status === "empty") {
      return (
        <div className="wb-chat-state" role="status">
          <h3 className="wb-chat-state__title">{EMPTY_COPY.title}</h3>
          <p>{emptyMessage ?? EMPTY_COPY.message}</p>
        </div>
      );
    }
    if (status === "error") {
      return (
        <div className="wb-chat-state" role="alert">
          <h3 className="wb-chat-state__title">{ERROR_COPY.title}</h3>
          <p>{errorMessage ?? ERROR_COPY.message}</p>
          {onRetry !== undefined ? (
            <Button
              variant="secondary"
              className="wb-chat-state__action"
              onClick={onRetry}
            >
              Retry
            </Button>
          ) : null}
        </div>
      );
    }

    // "ready" and "read-only" both render the transcript.
    const readOnly = status === "read-only";
    return (
      <>
        {renderTranscript()}
        {readOnly ? (
          <InlineAlert
            tone="info"
            role="status"
            className="wb-chat-panel__read-only"
          >
            <strong>Read-only:</strong>{" "}
            {readOnlyReason ?? "Replies are currently disabled."}
          </InlineAlert>
        ) : onSend !== undefined ? (
          <ChatComposer
            onSend={onSend}
            sending={sending}
            disabled={composerDisabled}
            placeholder={composerPlaceholder}
            errorMessage={sendErrorMessage}
          />
        ) : null}
      </>
    );
  };

  return (
    <section className="wb-chat-panel" aria-label={label}>
      {renderHeader()}
      {renderBody()}
    </section>
  );
}
