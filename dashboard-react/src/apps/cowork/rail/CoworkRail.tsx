/**
 * The Review | Chat rail (section 5.1). This is the mount seam the view frame
 * wires in place of the rail placeholder: it owns the two tabs, the Review panel
 * (section 5.5, variant-A-hybrid), and the Chat panel. Chat reuses the house
 * conversation machinery wholesale through ChatPanel (mode pane, one
 * conversation per document), so no new chat infrastructure is added here.
 */

import { useEffect, useMemo, useState } from "react";

import { HelpTarget, type HelpContent } from "../../../dashboard/help";
import {
  ChatPanel,
  deriveAgentActivity,
  useChatConversation,
  type ChatConversationProvider,
  type ChatPanelStatus,
} from "../../../widget-library/chat";
import {
  CoworkChatPanel,
  type CoworkChatAnnotations,
  type ScrollAnchorTarget,
} from "../chat";
import { loadChatDraft, saveChatDraft } from "../guards";
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
    "Lists the tracked edits, flags, and claims the agent raised on this document. Decide on each one, then submit them together as a single sitting.",
};

/** Hover-help for the Chat tab, surfaced when app-shell help mode is on. */
const CHAT_TAB_HELP: HelpContent = {
  summary: "Talk to the agent about this document.",
  details:
    "The document conversation. Ask a question, leave feedback on a highlighted passage, and read the agent's replies without leaving the review.",
};

export interface CoworkRailProps {
  readonly documentId: string;
  readonly reviewProvider: ReviewRailProvider;
  readonly chatProvider: ChatConversationProvider;
  readonly conversationId: string;
  /** Injectable rail store, else one is created for this rail instance. */
  readonly store?: RailStore;
  readonly storage?: Storage;
  readonly anchorRects?: AnchorRectSource;
  readonly queueBindings?: QueueBindings;
  readonly narrow?: boolean;
  readonly initialTab?: RailTab;
  /**
   * The document linkage store for the Chat tab. When supplied the tab renders the richer
   * Co-work chat panel (feedback span links and routing-note delivery status) instead of the
   * plain house chat panel, so the demo and test paths keep the plain panel by omitting it.
   */
  readonly chatAnnotations?: CoworkChatAnnotations;
  /** The scroll-to-passage seam for a feedback span link, wired by the surface. */
  readonly onScrollToChatAnchor?: (target: ScrollAnchorTarget) => void;
}

export function CoworkRail(props: CoworkRailProps) {
  const [store] = useState(
    () => props.store ?? new RailStore({ tab: props.initialTab ?? "review" }),
  );
  const tab = useRailState(store, (state) => state.tab);

  const chat = useChatConversation(props.chatProvider, props.conversationId);
  const messages = chat.snapshot?.messages ?? [];

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

  const chatStatus: ChatPanelStatus = useMemo(() => {
    if (chat.status === "loading") return "loading";
    if (chat.status === "error") return "error";
    return chat.snapshot?.status === "closed" ? "read-only" : "ready";
  }, [chat.status, chat.snapshot?.status]);

  const agentActivity =
    chat.snapshot !== null ? deriveAgentActivity(chat.snapshot) : "idle";

  return (
    <div className="wb-cowork-rail">
      <div
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
            onClick={() => store.setTab("chat")}
          >
            Chat
            {unread ? (
              <span className="wb-cowork-rail__unread">
                <span className="wb-visually-hidden">unread reply</span>
              </span>
            ) : null}
          </button>
        </HelpTarget>
      </div>

      <div
        role="tabpanel"
        id="wb-cowork-rail-panel-review"
        aria-labelledby="wb-cowork-rail-tab-review"
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
        aria-labelledby="wb-cowork-rail-tab-chat"
        className="wb-cowork-rail__tabpanel"
        hidden={tab !== "chat"}
      >
        {props.chatAnnotations !== undefined ? (
          <CoworkChatPanel
            provider={props.chatProvider}
            conversationId={props.conversationId}
            annotations={props.chatAnnotations}
            onScrollToAnchor={props.onScrollToChatAnchor}
            composerInitialValue={
              loadChatDraft(
                props.storage ?? window.localStorage,
                props.conversationId,
              ) ?? undefined
            }
            onComposerDraftChange={(text) =>
              saveChatDraft(
                props.storage ?? window.localStorage,
                props.conversationId,
                text,
              )
            }
          />
        ) : (
          <ChatPanel
            title="Document conversation"
            status={chatStatus}
            messages={messages}
            agentActivity={agentActivity}
            onSend={(value) => chat.send(value)}
            sending={chat.sending}
            sendErrorMessage={chat.sendError ?? undefined}
            onRetry={chat.retry}
            noMessagesLabel="No messages yet. Ask the document agent anything."
            initialValue={
              loadChatDraft(
                props.storage ?? window.localStorage,
                props.conversationId,
              ) ?? undefined
            }
            onDraftChange={(text) =>
              saveChatDraft(
                props.storage ?? window.localStorage,
                props.conversationId,
                text,
              )
            }
          />
        )}
      </div>
    </div>
  );
}

export default CoworkRail;
