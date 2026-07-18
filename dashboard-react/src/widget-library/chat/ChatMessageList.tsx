import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { Button, InlineAlert } from "../../ui";
import { formatTime } from "../shared";
import type { ChatAgentActivity, ChatMessage } from "./contracts";
import "./styles.css";

/** Distance in px from the bottom within which the view counts as pinned. */
const PIN_THRESHOLD = 24;

export interface ChatMessageListProps {
  readonly messages: readonly ChatMessage[];
  /** Accessible name for the transcript log region. */
  readonly label?: string;
  /** Drives the typing indicator and agent-stopped notice. */
  readonly agentActivity?: ChatAgentActivity;
  /** Inline answer handler for pending boolean and choice questions. */
  readonly onRespond?: (value: string, inReplyTo?: string) => void;
  /**
   * Seed the unread boundary on mount. Messages from this id onward start
   * unread and the view opens locked at that boundary rather than the bottom.
   */
  readonly initialUnreadFromMessageId?: string | null;
  /** Fired when the reader reaches the latest message and unread clears. */
  readonly onReachLatest?: () => void;
  /** Shown when the conversation has no messages yet. */
  readonly emptyLabel?: string;
}

function authorName(message: ChatMessage): string {
  if (message.author === "user") return "You";
  if (message.author === "system") return message.authorLabel ?? "System";
  return message.authorLabel ?? "Assistant";
}

function seedReadCount(
  messages: readonly ChatMessage[],
  initialUnreadFromMessageId: string | null | undefined,
): number {
  if (initialUnreadFromMessageId != null) {
    const index = messages.findIndex(
      (message) => message.id === initialUnreadFromMessageId,
    );
    if (index >= 0) return index;
  }
  return messages.length;
}

export function ChatMessageList({
  messages,
  label,
  agentActivity = "idle",
  onRespond,
  initialUnreadFromMessageId,
  onReachLatest,
  emptyLabel = "No messages yet.",
}: ChatMessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const boundaryRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState<boolean>(
    () => initialUnreadFromMessageId == null,
  );
  const pinnedRef = useRef(pinned);
  const [readCount, setReadCount] = useState<number>(() =>
    seedReadCount(messages, initialUnreadFromMessageId),
  );

  useEffect(() => {
    pinnedRef.current = pinned;
  }, [pinned]);

  const messageCount = messages.length;
  const unreadCount = Math.max(0, messageCount - readCount);
  // A boundary at index 0 is legitimate: a seeded id matching the first
  // message means the whole transcript is unread, and the separator renders
  // above it.
  const boundaryIndex =
    unreadCount > 0 && readCount < messageCount ? readCount : -1;

  // Autoscroll while pinned. When the reader has scrolled up (scroll lock) the
  // view holds position and the arriving messages accumulate as unread. Keyed
  // on the message COUNT: the house store appends discrete messages, so a
  // last message whose content grows in place will not re-stick the view.
  useLayoutEffect(() => {
    if (!pinnedRef.current) return;
    const element = scrollRef.current;
    if (element !== null) element.scrollTop = element.scrollHeight;
    setReadCount(messageCount);
  }, [messageCount]);

  // On a seeded-unread mount, open at the boundary rather than the bottom.
  useLayoutEffect(() => {
    if (pinnedRef.current) return;
    const boundary = boundaryRef.current;
    if (boundary !== null && typeof boundary.scrollIntoView === "function") {
      boundary.scrollIntoView({ block: "start" });
    } else if (scrollRef.current !== null) {
      scrollRef.current.scrollTop = 0;
    }
    // Intentionally mount-only, later positioning is scroll and pin driven.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const prevUnreadRef = useRef(unreadCount);
  useEffect(() => {
    if (prevUnreadRef.current > 0 && unreadCount === 0) onReachLatest?.();
    prevUnreadRef.current = unreadCount;
  }, [unreadCount, onReachLatest]);

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

  const renderAffordances = (message: ChatMessage): ReactNode => {
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
                  ref={boundaryRef}
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
                  {renderAffordances(message)}
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
