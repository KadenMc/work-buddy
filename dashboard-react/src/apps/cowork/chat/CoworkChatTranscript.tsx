// The Co-work transcript. It mirrors the house ChatMessageList behaviour the
// Chat tab relies on (author-attributed bubbles, the scroll-pinned unread
// boundary and jump-to-latest, the typing indicator, the agent-stopped notice)
// and adds the two document affordances the house list cannot host per item: a
// scroll-to-passage control on a feedback message anchored to a span, and the
// delivery status of a routing note sent to the document agent. The scroll-to
// seam is a callback prop, so this component never imports the editor.

import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { Button, InlineAlert } from "../../../ui";
import { formatTime } from "../../../widget-library/shared";
import type {
  ChatAgentActivity,
  ChatMessage,
} from "../../../widget-library/chat";
import type {
  ResolvedSpanLink,
  RoutingDelivery,
  ScrollAnchorTarget,
} from "./contracts";
import "./styles.css";

/** Distance in px from the bottom within which the view counts as pinned. */
const PIN_THRESHOLD = 24;

export interface CoworkChatTranscriptProps {
  readonly messages: readonly ChatMessage[];
  /** Accessible name for the transcript log region. */
  readonly label?: string;
  /** Drives the typing indicator and the agent-stopped notice. */
  readonly agentActivity?: ChatAgentActivity;
  /** Span links keyed by message id (resolveSpanLinks output). */
  readonly spanLinks?: ReadonlyMap<string, ResolvedSpanLink>;
  /** Routing-note deliveries rendered as status notices after the transcript. */
  readonly routing?: readonly RoutingDelivery[];
  /** The scroll-to-passage seam. Not a direct editor import. */
  readonly onScrollToAnchor?: (target: ScrollAnchorTarget) => void;
  /** Inline answer handler for pending boolean and choice questions. */
  readonly onRespond?: (value: string, inReplyTo?: string) => void;
  /** Dismiss one routing-note delivery notice by its id. */
  readonly onDismissRouting?: (id: string) => void;
  /** Shown when the conversation has no messages yet. */
  readonly emptyLabel?: string;
}

function authorName(message: ChatMessage): string {
  if (message.author === "user") return "You";
  if (message.author === "system") return message.authorLabel ?? "System";
  return message.authorLabel ?? "Assistant";
}

function routingLabel(delivery: RoutingDelivery): string {
  const target =
    delivery.verb === "redirect" ? "Redirect" : "Endorsement";
  if (delivery.state === "delivered") {
    return `${target} sent to the document agent.`;
  }
  return `${target} could not be delivered to the document agent.`;
}

export function CoworkChatTranscript({
  messages,
  label,
  agentActivity = "idle",
  spanLinks,
  routing,
  onScrollToAnchor,
  onRespond,
  onDismissRouting,
  emptyLabel = "No messages yet.",
}: CoworkChatTranscriptProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);
  const pinnedRef = useRef(pinned);
  const [readCount, setReadCount] = useState<number>(() => messages.length);

  useEffect(() => {
    pinnedRef.current = pinned;
  }, [pinned]);

  const messageCount = messages.length;
  const unreadCount = Math.max(0, messageCount - readCount);
  const boundaryIndex =
    unreadCount > 0 && readCount < messageCount ? readCount : -1;

  // Autoscroll while pinned. When the reader has scrolled up the view holds
  // position and arriving messages accrue as unread. Keyed on the message COUNT
  // so a last message that grows in place does not re-stick the view.
  useLayoutEffect(() => {
    if (!pinnedRef.current) return;
    const element = scrollRef.current;
    if (element !== null) element.scrollTop = element.scrollHeight;
    setReadCount(messageCount);
  }, [messageCount]);

  const handleScroll = () => {
    const element = scrollRef.current;
    if (element === null) return;
    const distance =
      element.scrollHeight - element.scrollTop - element.clientHeight;
    const nowPinned = distance <= PIN_THRESHOLD;
    setPinned(nowPinned);
    pinnedRef.current = nowPinned;
    if (nowPinned) setReadCount(messageCount);
  };

  const jumpToLatest = () => {
    const element = scrollRef.current;
    if (element !== null) element.scrollTop = element.scrollHeight;
    setPinned(true);
    pinnedRef.current = true;
    setReadCount(messageCount);
  };

  const renderQuestionAffordances = (message: ChatMessage): ReactNode => {
    if (onRespond === undefined) return null;
    if (message.pending !== true || message.question === undefined) return null;
    const { question } = message;
    if (question.responseType === "boolean") {
      return (
        <div className="wb-chat-msg__choices">
          <Button
            variant="secondary"
            size="small"
            className="wb-chat-choice"
            onClick={() => onRespond("true", message.id)}
          >
            Yes
          </Button>
          <Button
            variant="secondary"
            size="small"
            className="wb-chat-choice"
            onClick={() => onRespond("false", message.id)}
          >
            No
          </Button>
        </div>
      );
    }
    if (question.responseType === "choice" && question.choices !== undefined) {
      return (
        <div className="wb-chat-msg__choices">
          {question.choices.map((choice) => (
            <Button
              key={choice.key}
              variant="secondary"
              size="small"
              className="wb-chat-choice"
              onClick={() => onRespond(choice.key, message.id)}
            >
              {choice.label}
            </Button>
          ))}
        </div>
      );
    }
    return null;
  };

  const renderSpanLink = (message: ChatMessage): ReactNode => {
    const link = spanLinks?.get(message.id);
    if (link === undefined) return null;
    const quote = link.target.anchor?.exact ?? "";
    const accessibleName =
      quote.length > 0
        ? `Jump to the passage "${quote}"`
        : "Jump to the anchored passage";
    return (
      <div className="wb-cowork-chat-msg__anchor">
        <Button
          variant="secondary"
          size="small"
          className="wb-cowork-chat-anchor-button"
          onClick={
            onScrollToAnchor === undefined
              ? undefined
              : () => onScrollToAnchor(link.target)
          }
          disabled={onScrollToAnchor === undefined}
          aria-label={accessibleName}
        >
          <span aria-hidden="true">Jump to passage</span>
        </Button>
      </div>
    );
  };

  return (
    <div className="wb-chat-list">
      <div
        ref={scrollRef}
        className="wb-chat-list__scroll"
        role="log"
        aria-label={label ?? "Conversation"}
        aria-live="polite"
        aria-relevant="additions"
        tabIndex={0}
        onScroll={handleScroll}
      >
        {messageCount === 0 ? (
          <p className="wb-chat-list__empty">{emptyLabel}</p>
        ) : (
          messages.map((message, index) => (
            <Fragment key={message.id}>
              {index === boundaryIndex ? (
                <div
                  className="wb-chat-list__unread"
                  role="separator"
                  aria-label="Start of unread messages"
                >
                  <span aria-hidden="true">New messages</span>
                </div>
              ) : null}
              <div
                className={`wb-chat-msg wb-chat-msg--${message.author}`}
                data-author={message.author}
              >
                <div className="wb-chat-msg__bubble">
                  <span className="wb-visually-hidden">
                    {authorName(message)}:
                  </span>
                  <span className="wb-chat-msg__content">{message.content}</span>
                  {renderSpanLink(message)}
                  {renderQuestionAffordances(message)}
                </div>
                {message.createdAt !== undefined ? (
                  <span className="wb-chat-msg__time">
                    {formatTime(message.createdAt)}
                  </span>
                ) : null}
              </div>
            </Fragment>
          ))
        )}

        {routing !== undefined && routing.length > 0 ? (
          <ul className="wb-cowork-chat-routing" aria-label="Routing notes">
            {routing.map((delivery) => (
              <li
                key={delivery.id}
                className="wb-cowork-chat-routing__item"
                data-state={delivery.state}
              >
                <InlineAlert
                  tone={delivery.state === "delivered" ? "info" : "danger"}
                  role="status"
                  className="wb-cowork-chat-routing__alert"
                >
                  <span aria-hidden="true">
                    {delivery.state === "delivered" ? "→ " : "! "}
                  </span>
                  {routingLabel(delivery)}
                  {delivery.reason !== undefined && delivery.reason.length > 0 ? (
                    <span className="wb-cowork-chat-routing__reason">
                      {" "}
                      {delivery.reason}
                    </span>
                  ) : null}
                </InlineAlert>
                {onDismissRouting !== undefined ? (
                  <Button
                    variant="secondary"
                    size="small"
                    className="wb-cowork-chat-routing__dismiss"
                    onClick={() => onDismissRouting(delivery.id)}
                    aria-label={`Dismiss ${delivery.verb} delivery notice`}
                  >
                    <span aria-hidden="true">Dismiss</span>
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {agentActivity === "thinking" ? (
        <div className="wb-chat-typing" role="status">
          <span className="wb-chat-typing__dots" aria-hidden="true">
            <span className="wb-chat-typing__dot" />
            <span className="wb-chat-typing__dot" />
            <span className="wb-chat-typing__dot" />
          </span>
          <span className="wb-visually-hidden">Assistant is typing</span>
        </div>
      ) : null}

      {agentActivity === "stopped" ? (
        <InlineAlert tone="danger" role="status" className="wb-chat-stopped">
          <span aria-hidden="true">■ </span>
          Agent stopped responding. Close this chat and start a new one to
          continue.
        </InlineAlert>
      ) : null}

      {unreadCount > 0 ? (
        <div className="wb-chat-list__jump">
          <Button
            variant="primary"
            size="small"
            className="wb-chat-list__jump-button"
            onClick={jumpToLatest}
          >
            {unreadCount} new {unreadCount === 1 ? "message" : "messages"} ·
            Jump to latest
          </Button>
        </div>
      ) : null}
    </div>
  );
}
