/**
 * The Review | Chat rail (section 5.1). This is the mount seam the view frame
 * wires in place of the rail placeholder: it owns the two tabs, the Review panel
 * (section 5.5, variant-A-hybrid), and the Chat panel. Chat reuses the house
 * conversation machinery wholesale through ChatPanel (mode pane, one
 * conversation per document), so no new chat infrastructure is added here.
 */

import { useEffect, useState } from "react";

import { HelpTarget, type HelpContent } from "../../../dashboard/help";
import { Button } from "../../../ui";
import {
  ChatPanel,
  deriveAgentActivity,
  useChatConversation,
  type ChatConversationProvider,
  type ChatMessage,
  type ChatPanelStatus,
} from "../../../widget-library/chat";
import {
  CoworkChatPanel,
  type CoworkChatAnnotations,
  type CoworkDocumentAgent,
  type ScrollAnchorTarget,
} from "../chat";
import {
  loadChatDraft,
  loadRailTab,
  saveChatDraft,
  saveRailTab,
} from "../guards";
import { ReviewPanel } from "./ReviewPanel";
import type { QueueBindings } from "./QueueView";
import type { AnchorRectSource, ReviewRailProvider } from "./provider";
import { RailStore, type RailTab } from "./store";
import { useRailState } from "./useRailState";
import "./styles.css";

/** Hover-help for the Review tab, surfaced when app-shell help mode is on. */
const REVIEW_TAB_HELP: HelpContent = {
  summary: "Review the agent's proposed changes.",
  details:
    "Agent suggestions and questions appear here. Review each item, then submit your decisions together.",
};

/** Hover-help for the Chat tab, surfaced when app-shell help mode is on. */
const CHAT_TAB_HELP: HelpContent = {
  summary: "Talk to the agent about this document.",
  details:
    "Ask a question, leave feedback on a highlighted passage, and read the agent's replies without leaving the document.",
};

const EMPTY_CHAT_MESSAGES: readonly ChatMessage[] = [];

export interface CoworkRailProps {
  readonly documentId: string;
  readonly reviewProvider: ReviewRailProvider;
  readonly chat: CoworkRailChat;
  /** Fired only for a present user click on the wide Chat tab. */
  readonly onChatSelected?: () => void;
  /** Injectable rail store, else one is created for this rail instance. */
  readonly store?: RailStore;
  readonly storage?: Storage;
  readonly anchorRects?: AnchorRectSource;
  readonly queueBindings?: QueueBindings;
  readonly narrow?: boolean;
  readonly initialTab?: RailTab;
  /** Narrow workspace peer tabs own Review / Chat selection when false. */
  readonly showTabs?: boolean;
  /**
   * The document linkage store for the Chat tab. When supplied the tab renders the richer
   * Co-work chat panel (feedback span links and routing-note delivery status) instead of the
   * plain house chat panel, so the demo and test paths keep the plain panel by omitting it.
   */
  readonly chatAnnotations?: CoworkChatAnnotations;
  /** The scroll-to-passage seam for a feedback span link, wired by the surface. */
  readonly onScrollToChatAnchor?: (target: ScrollAnchorTarget) => void;
}

export type CoworkRailChat =
  | {
      readonly kind: "ready";
      readonly provider: ChatConversationProvider;
      readonly conversationId: string;
      /** Stable document-scoped key, available before a conversation id exists. */
      readonly draftStorageId: string;
      readonly agent: CoworkDocumentAgent;
      readonly ensuringAgent?: boolean;
      readonly ensureError?: string | null;
      readonly onEnsureAgent: () => void;
    }
  | {
      readonly kind: "loading";
      readonly draftStorageId: string;
    }
  | {
      readonly kind: "ensuring";
      readonly draftStorageId: string;
    }
  | {
      readonly kind: "idle";
      readonly draftStorageId: string;
      readonly onStart: () => void;
    }
  | {
      readonly kind: "error";
      readonly draftStorageId: string;
      readonly error: string;
      readonly action?: "start" | "restart";
      readonly onRetry: () => void;
    };

function CoworkConversationGate({ chat }: { readonly chat: Exclude<CoworkRailChat, { kind: "ready" }> }) {
  if (chat.kind === "loading" || chat.kind === "ensuring") {
    return (
      <section className="wb-chat-panel" aria-label="Chat about this document">
        <div className="wb-chat-state" role="status">
          <span className="wb-spinner" aria-hidden="true" />
          <h3 className="wb-chat-state__title">
            {chat.kind === "loading"
              ? "Loading chat"
              : "Starting chat"}
          </h3>
          <p>
            {chat.kind === "loading"
              ? "Checking for earlier messages."
              : "This may take a moment."}
          </p>
        </div>
      </section>
    );
  }
  if (chat.kind === "idle") {
    return (
      <section className="wb-chat-panel" aria-label="Chat about this document">
        <div className="wb-chat-state" role="status">
          <h3 className="wb-chat-state__title">Chat about this document</h3>
          <p>Start chat when you’re ready.</p>
          <Button
            variant="secondary"
            className="wb-chat-state__action"
            onClick={chat.onStart}
          >
            Start chat
          </Button>
        </div>
      </section>
    );
  }
  return (
    <section className="wb-chat-panel" aria-label="Chat about this document">
      <div className="wb-chat-state" role="alert">
        <h3 className="wb-chat-state__title">
          Chat could not connect
        </h3>
        <p>{chat.error}</p>
        <Button
          variant="secondary"
          className="wb-chat-state__action"
          onClick={chat.onRetry}
        >
          {chat.action === "restart" ? "Restart chat" : "Start chat"}
        </Button>
      </div>
    </section>
  );
}

function PlainCoworkChat({
  chat,
  storage,
  onMessages,
}: {
  readonly chat: Extract<CoworkRailChat, { kind: "ready" }>;
  readonly storage: Storage;
  readonly onMessages: (messages: readonly ChatMessage[]) => void;
}) {
  const conversation = useChatConversation(chat.provider, chat.conversationId);
  const messages = conversation.snapshot?.messages ?? EMPTY_CHAT_MESSAGES;
  useEffect(() => onMessages(messages), [messages, onMessages]);
  const status: ChatPanelStatus =
    conversation.status === "loading"
      ? "loading"
      : conversation.status === "error"
        ? "error"
        : conversation.snapshot?.status === "closed"
          ? "read-only"
          : "ready";
  const agentActivity =
    conversation.snapshot !== null
      ? deriveAgentActivity(conversation.snapshot)
      : "idle";
  return (
    <ChatPanel
      title="Chat about this document"
      status={status}
      messages={messages}
      agentActivity={agentActivity}
      onSend={(value) => conversation.send(value)}
      sending={conversation.sending}
      sendErrorMessage={conversation.sendError ?? undefined}
      errorMessage={conversation.error ?? undefined}
      onRetry={conversation.retry}
      noMessagesLabel="No messages yet. Ask anything about this document."
      initialValue={
        loadChatDraft(storage, chat.draftStorageId) ?? undefined
      }
      onDraftChange={(text) =>
        saveChatDraft(storage, chat.draftStorageId, text)
      }
    />
  );
}

export function CoworkRail(props: CoworkRailProps) {
  const [store] = useState(() => {
    if (props.store) return props.store;
    // The injecting site owns its own persistence, so only a rail-created store seeds its tab
    // from storage and mirrors later changes back. Precedence: an explicit initialTab wins,
    // then the retained tab, then the Review default.
    const storage = props.storage ?? window.localStorage;
    const initialTab =
      props.initialTab ?? loadRailTab(storage, props.documentId) ?? "review";
    return new RailStore(
      { tab: initialTab },
      { onTabChange: (tab) => saveRailTab(storage, props.documentId, tab) },
    );
  });
  const tab = useRailState(store, (state) => state.tab);
  const [messages, setMessages] = useState<readonly ChatMessage[]>([]);
  const conversationId =
    props.chat.kind === "ready" ? props.chat.conversationId : null;
  useEffect(() => setMessages([]), [conversationId]);

  // Unread dot: an assistant message arrived while the Review tab was showing.
  const [seenCount, setSeenCount] = useState(0);
  useEffect(() => {
    if (tab === "chat") setSeenCount(messages.length);
  }, [tab, messages.length]);
  const unread =
    tab !== "chat" &&
    messages.length > seenCount &&
    messages
      .slice(seenCount)
      .some((message) => message.author === "assistant");

  return (
    <div className="wb-cowork-rail">
      {props.showTabs !== false ? <div
        className="wb-cowork-rail__tabs"
        role="tablist"
        aria-label="Review and chat"
      >
        <HelpTarget content={REVIEW_TAB_HELP} placement="bottom start">
          <button
            type="button"
            role="tab"
            id="wb-cowork-rail-tab-review"
            className="wb-cowork-rail__tab"
            aria-selected={tab === "review"}
            aria-controls="wb-cowork-rail-panel-review"
            onClick={() => store.setTab("review")}
          >
            Review
          </button>
        </HelpTarget>
        <HelpTarget content={CHAT_TAB_HELP} placement="bottom">
          <button
            type="button"
            role="tab"
            id="wb-cowork-rail-tab-chat"
            className="wb-cowork-rail__tab"
            aria-selected={tab === "chat"}
            aria-controls="wb-cowork-rail-panel-chat"
            onClick={() => {
              store.setTab("chat");
              props.onChatSelected?.();
            }}
          >
            Chat
            {unread ? (
              <span className="wb-cowork-rail__unread">
                <span className="wb-visually-hidden">unread reply</span>
              </span>
            ) : null}
          </button>
        </HelpTarget>
      </div> : null}

      <div
        role="tabpanel"
        id="wb-cowork-rail-panel-review"
        aria-labelledby={
          props.showTabs === false
            ? "wb-cowork-mobile-tab-review"
            : "wb-cowork-rail-tab-review"
        }
        className="wb-cowork-rail__tabpanel"
        hidden={tab !== "review"}
      >
        <ReviewPanel
          provider={props.reviewProvider}
          store={store}
          documentId={props.documentId}
          storage={props.storage}
          anchorRects={props.anchorRects}
          queueBindings={props.queueBindings}
          narrow={props.narrow}
        />
      </div>

      <div
        role="tabpanel"
        id="wb-cowork-rail-panel-chat"
        aria-labelledby={
          props.showTabs === false
            ? "wb-cowork-mobile-tab-chat"
            : "wb-cowork-rail-tab-chat"
        }
        className="wb-cowork-rail__tabpanel"
        hidden={tab !== "chat"}
      >
        {props.chat.kind !== "ready" ? (
          <CoworkConversationGate chat={props.chat} />
        ) : props.chatAnnotations !== undefined ? (
          <CoworkChatPanel
            provider={props.chat.provider}
            conversationId={props.chat.conversationId}
            annotations={props.chatAnnotations}
            onScrollToAnchor={props.onScrollToChatAnchor}
            agent={props.chat.agent}
            ensuringAgent={props.chat.ensuringAgent}
            ensureError={props.chat.ensureError}
            onEnsureAgent={props.chat.onEnsureAgent}
            onMessagesChange={setMessages}
            composerInitialValue={
              loadChatDraft(
                props.storage ?? window.localStorage,
                props.chat.draftStorageId,
              ) ?? undefined
            }
            onComposerDraftChange={(text) =>
              saveChatDraft(
                props.storage ?? window.localStorage,
                props.chat.draftStorageId,
                text,
              )
            }
          />
        ) : (
          <PlainCoworkChat
            chat={props.chat}
            storage={props.storage ?? window.localStorage}
            onMessages={setMessages}
          />
        )}
      </div>
    </div>
  );
}

export default CoworkRail;
