import type { ReactNode } from "react";

import { Button, InlineAlert } from "../../ui";
import { ChatComposer } from "./ChatComposer";
import { ChatExecutionPicker } from "./ChatExecutionPicker";
import { ChatMessageList } from "./ChatMessageList";
import type {
  ChatAgentActivity,
  ChatMessage,
  ChatPanelStatus,
} from "./contracts";
import type { ChatExecutionControl } from "./useChatExecutionProfile";
import "./styles.css";

export interface ChatPanelStateAction {
  readonly label: string;
  readonly onAction: () => void;
  readonly pending?: boolean;
  /** Opt in when this host action launches or replaces the selected runtime. */
  readonly requiresExecution?: boolean;
}

export type ChatPanelStateKind = "loading" | "empty" | "error";

export interface ChatPanelStateProps {
  /** Accessible name for the complete panel. */
  readonly label?: string;
  readonly kind: ChatPanelStateKind;
  readonly title: string;
  readonly detail?: ReactNode;
  readonly action?: ChatPanelStateAction;
  readonly header?: ReactNode;
  readonly execution?: ChatExecutionControl;
  readonly executionDisabled?: boolean;
}

/**
 * Canonical full-panel state for a chat whose conversation is not yet ready.
 * Hosts map their lifecycle into this primitive instead of recreating panel
 * markup or depending on the chat package's private CSS classes.
 */
export function ChatPanelState({
  label = "Conversation",
  kind,
  title,
  detail,
  action,
  header,
  execution,
  executionDisabled = false,
}: ChatPanelStateProps) {
  return (
    <section className="wb-chat-panel" aria-label={label}>
      {header}
      <div className="wb-chat-state">
        <div
          className="wb-chat-state__copy"
          role={kind === "error" ? "alert" : "status"}
        >
          {kind === "loading" ? (
            <span className="wb-spinner" aria-hidden="true" />
          ) : null}
          <h3 className="wb-chat-state__title">{title}</h3>
          {detail === undefined ? null : <p>{detail}</p>}
        </div>
        {execution === undefined ? null : (
          <ChatExecutionPicker
            control={execution}
            disabled={executionDisabled}
            className="wb-chat-state__execution"
          />
        )}
        {action === undefined ? null : (
          <Button
            variant="secondary"
            className="wb-chat-state__action"
            onClick={action.onAction}
            disabled={
              action.pending === true ||
              (action.requiresExecution === true &&
                execution !== undefined &&
                (execution.status !== "ready" ||
                  execution.selecting ||
                  !execution.currentAvailable ||
                  execution.snapshot?.readOnly === true))
            }
          >
            {action.label}
          </Button>
        )}
      </div>
    </section>
  );
}

export interface ChatPanelProps {
  /** Host presentation state, mirroring the dashboard host-state contract. */
  readonly status?: ChatPanelStatus;
  readonly messages: readonly ChatMessage[];
  /** Accessible name for the panel and the default header text. */
  readonly title?: string;
  /** Header slot. Overrides the default title bar when provided. */
  readonly header?: ReactNode;
  readonly agentActivity?: ChatAgentActivity;
  /** Additive content rendered inside each message after its canonical text. */
  readonly renderMessageAccessory?: (message: ChatMessage) => ReactNode;
  /** Additive content rendered after all canonical messages in the scroller. */
  readonly transcriptAppendix?: ReactNode;
  /** Extra host-level reason to disable structured question responses. */
  readonly responsesDisabled?: boolean;
  /** Whether the shared passive stopped-agent notice should be rendered. */
  readonly showStoppedNotice?: boolean;
  /**
   * Send intent for freeform messages and inline question answers. Inline
   * answers pass the answered question's message id as inReplyTo.
   */
  onSend?(value: string, inReplyTo?: string): void | Promise<void>;
  readonly sending?: boolean;
  readonly sendErrorMessage?: string;
  readonly composerDisabled?: boolean;
  readonly composerPlaceholder?: string;
  /** Additive context controls rendered by the shared composer. */
  readonly composerAccessory?: ReactNode;
  /** Compact additive context rendered in the shared composer footer. */
  readonly composerFooterAccessory?: ReactNode;
  /** Seed the composer draft once on mount, e.g. from a retained unsent draft. */
  readonly initialValue?: string;
  /**
   * Observe the live composer draft. Fired on every edit and with an empty
   * string after a successful send, so a host can retain the unsent draft and
   * arm an unsaved-work guard. Forwarded to the composer, which still owns the
   * text state.
   */
  onDraftChange?(value: string): void;
  /** Reason shown in place of the composer when status is "read-only". */
  readonly readOnlyReason?: string;
  /** Full-panel copy for the "empty" host state. */
  readonly emptyMessage?: string;
  /** Full-panel copy for the "error" host state. */
  readonly errorMessage?: string;
  readonly onRetry?: () => void;
  readonly initialUnreadFromMessageId?: string | null;
  readonly onReachLatest?: () => void;
  /** Changes after a locally-authored send so that turn is brought into view. */
  readonly revealLatestMessageToken?: number;
  /** Empty-transcript copy inside a ready conversation. */
  readonly noMessagesLabel?: string;
  /** Server-authoritative provider/model selection for the next agent turn. */
  readonly execution?: ChatExecutionControl;
}

interface StateCopy {
  readonly title: string;
  readonly message: string;
}

const LOADING_COPY: StateCopy = {
  title: "Loading chat",
  message: "Loading messages.",
};
const EMPTY_COPY: StateCopy = {
  title: "No chat yet",
  message: "Start a conversation to see messages here.",
};
const ERROR_COPY: StateCopy = {
  title: "Chat couldn’t load",
  message: "Try again.",
};

export function ChatPanel({
  status = "ready",
  messages,
  title,
  header,
  agentActivity = "idle",
  renderMessageAccessory,
  transcriptAppendix,
  responsesDisabled = false,
  showStoppedNotice = true,
  onSend,
  sending = false,
  sendErrorMessage,
  composerDisabled = false,
  composerPlaceholder,
  composerAccessory,
  composerFooterAccessory,
  initialValue,
  onDraftChange,
  readOnlyReason,
  emptyMessage,
  errorMessage,
  onRetry,
  initialUnreadFromMessageId,
  onReachLatest,
  revealLatestMessageToken,
  noMessagesLabel,
  execution,
}: ChatPanelProps) {
  const label = title ?? "Conversation";
  const readOnly =
    status === "read-only" || execution?.snapshot?.readOnly === true;
  const structuredResponsesDisabled =
    responsesDisabled ||
    readOnly ||
    agentActivity === "thinking" ||
    composerDisabled ||
    execution?.selecting === true ||
    sending;

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
      renderMessageAccessory={renderMessageAccessory}
      transcriptAppendix={transcriptAppendix}
      onRespond={
        onSend === undefined
          ? undefined
          : (value, inReplyTo) => {
              // A failed inline answer surfaces through sendErrorMessage from
              // the container (the hook records it before rethrowing). The
              // catch prevents an unhandled rejection on this path.
              void Promise.resolve(onSend(value, inReplyTo)).catch(() => {});
            }
      }
      responsesDisabled={structuredResponsesDisabled}
      showStoppedNotice={showStoppedNotice && !readOnly}
      initialUnreadFromMessageId={initialUnreadFromMessageId}
      onReachLatest={onReachLatest}
      revealLatestMessageToken={revealLatestMessageToken}
      emptyLabel={noMessagesLabel}
    />
  );

  const renderBody = () => {
    // "ready" and "read-only" both render the transcript.
    return (
      <>
        {renderTranscript()}
        {readOnly ? (
          <div className="wb-chat-panel__input-region">
            {execution === undefined ? null : (
              <ChatExecutionPicker control={execution} readOnly />
            )}
            <InlineAlert
              tone="info"
              role="status"
              className="wb-chat-panel__read-only"
            >
              <strong>Read-only:</strong>{" "}
              {readOnlyReason ?? "Replies are currently disabled."}
            </InlineAlert>
          </div>
        ) : onSend !== undefined ? (
          <ChatComposer
            onSend={onSend}
            sending={sending}
            submissionDisabled={agentActivity === "thinking"}
            disabled={composerDisabled === true}
            placeholder={composerPlaceholder}
            errorMessage={sendErrorMessage}
            initialValue={initialValue}
            onDraftChange={onDraftChange}
            execution={execution}
            accessory={composerAccessory}
            footerAccessory={composerFooterAccessory}
          />
        ) : execution === undefined ? null : (
          <ChatExecutionPicker control={execution} />
        )}
      </>
    );
  };

  if (status === "loading" || status === "empty" || status === "error") {
    const copy =
      status === "loading"
        ? LOADING_COPY
        : status === "empty"
          ? EMPTY_COPY
          : ERROR_COPY;
    return (
      <ChatPanelState
        label={label}
        kind={status}
        title={copy.title}
        detail={
          status === "empty"
            ? emptyMessage ?? copy.message
            : status === "error"
              ? errorMessage ?? copy.message
              : copy.message
        }
        action={
          status === "error" && onRetry !== undefined
            ? { label: "Retry", onAction: onRetry }
            : undefined
        }
        header={renderHeader()}
        execution={execution}
        executionDisabled={sending || agentActivity === "thinking"}
      />
    );
  }

  return (
    <section className="wb-chat-panel" aria-label={label}>
      {renderHeader()}
      {renderBody()}
    </section>
  );
}
