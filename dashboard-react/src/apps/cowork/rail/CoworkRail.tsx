/**
 * The Review | Truth | Chat rail. This is the mount seam the view frame wires
 * in place of the rail placeholder: it owns all three tabs and their panels.
 * Every ready Chat path
 * uses the shared ConversationChat surface through a thin Co-work adapter.
 */

import { useEffect, useRef, useState, type KeyboardEvent, type RefCallback } from "react";

import { HelpTarget, type HelpContent } from "../../../dashboard/help";
import {
  type ChatConversationProvider,
  type ChatExecutionControl,
  type ChatMessage,
  ChatPanelState,
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
import type { CoworkShortcutBindings } from "../keyboard";
import {
  TruthAttentionFeed,
  TruthPanel,
  type TruthEditorIntegration,
  type TruthRailProvider,
  type TruthStore as CoworkTruthStore,
} from "../truth";
import { ReviewPanel } from "./ReviewPanel";
import type { VerificationRecheckIntent } from "./contracts";
import type { ReviewAnchorController, ReviewRailProvider } from "./provider";
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

/** Hover-help for the Truth tab without adding explanatory chrome. */
const TRUTH_TAB_HELP: HelpContent = {
  summary: "See what this work rests on.",
  details:
    "Inspect claims, where the document expresses them, their evidence, provenance, and lifecycle. Truth also hosts contextual claim management.",
};

const RAIL_TABS: readonly RailTab[] = ["review", "truth", "chat"];

export interface CoworkRailProps {
  readonly documentId: string;
  readonly storeId?: string;
  readonly truth?: {
    readonly provider: TruthRailProvider;
    readonly store: CoworkTruthStore;
    readonly editor?: TruthEditorIntegration;
    readonly readOnly?: boolean;
  };
  readonly reviewProvider: ReviewRailProvider;
  readonly chat: CoworkRailChat;
  /** Fired only for a present user click on the wide Chat tab. */
  readonly onChatSelected?: () => void;
  /** Injectable rail store, else one is created for this rail instance. */
  readonly store?: RailStore;
  readonly storage?: Storage;
  /** Device-local continuity for the Review tab's own scroll container. */
  readonly reviewScrollRef?: RefCallback<HTMLElement>;
  /** Saves and detaches Review before a view change can clamp its geometry. */
  readonly onReviewScrollWillDetach?: () => void;
  readonly truthScrollRef?: RefCallback<HTMLElement>;
  readonly onTruthScrollWillDetach?: () => void;
  readonly reviewAnchors?: ReviewAnchorController;
  readonly shortcutBindings?: CoworkShortcutBindings;
  readonly initialTab?: RailTab;
  /** Whether the containing workspace currently exposes the Review pane. */
  readonly reviewVisible?: boolean;
  readonly truthVisible?: boolean;
  /** Narrow workspace peer tabs own Review / Truth / Chat selection when false. */
  readonly showTabs?: boolean;
  /** Optional document linkage for passage accessories and routing notices. */
  readonly chatAnnotations?: CoworkChatAnnotations;
  /** The scroll-to-passage seam for a feedback span link, wired by the surface. */
  readonly onScrollToChatAnchor?: (target: ScrollAnchorTarget) => void;
  /** Shared provider/model selection, available before a conversation starts. */
  readonly chatExecution?: ChatExecutionControl;
  /** Opens one exact, server-derived correction recheck intent in Verify. */
  readonly onRecheckIntent?: (
    intent: VerificationRecheckIntent,
  ) => void | Promise<void>;
}

export type CoworkRailChat =
  | {
      readonly kind: "ready";
      readonly provider: ChatConversationProvider;
      readonly conversationId: string;
      /** Stable document-scoped key, available before a conversation id exists. */
      readonly draftStorageId: string;
      readonly agent: CoworkDocumentAgent;
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
    }
  | {
      readonly kind: "error";
      readonly draftStorageId: string;
      readonly error: string;
    };

function CoworkConversationGate({
  chat,
  execution,
}: {
  readonly chat: Exclude<CoworkRailChat, { kind: "ready" }>;
  readonly execution?: ChatExecutionControl;
}) {
  if (chat.kind === "loading" || chat.kind === "ensuring") {
    return (
      <ChatPanelState
        label="Chat about this document"
        kind="loading"
        title={chat.kind === "loading" ? "Loading chat…" : "Starting chat…"}
        detail={
          chat.kind === "loading" ? "Checking for messages." : undefined
        }
        execution={execution}
        executionDisabled
      />
    );
  }
  if (chat.kind === "idle") {
    return (
      <ChatPanelState
        label="Chat about this document"
        kind="loading"
        title="Preparing chat…"
        execution={execution}
        executionDisabled
      />
    );
  }
  return (
    <ChatPanelState
      label="Chat about this document"
      kind="error"
      title="Chat isn’t available."
      detail={chat.error}
      execution={execution}
      executionDisabled
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
      props.initialTab ??
      loadRailTab(storage, props.documentId, props.storeId) ??
      "review";
    return new RailStore(
      { tab: initialTab },
      {
        onTabChange: (tab) =>
          saveRailTab(storage, props.documentId, tab, props.storeId),
      },
    );
  });
  const tab = useRailState(store, (state) => state.tab);
  const tabRefs = useRef<Partial<Record<RailTab, HTMLButtonElement | null>>>({});
  const reviewActive = tab === "review" && props.reviewVisible !== false;
  const [messages, setMessages] = useState<readonly ChatMessage[]>([]);
  const conversationId =
    props.chat.kind === "ready" ? props.chat.conversationId : null;
  useEffect(() => setMessages([]), [conversationId]);

  // Unread dot: an assistant message arrived while another rail tab was showing.
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

  const selectRailTab = (next: RailTab): void => {
    if (tab === "review" && next !== "review") {
      props.onReviewScrollWillDetach?.();
    }
    if (tab === "truth" && next !== "truth") {
      props.onTruthScrollWillDetach?.();
    }
    store.setTab(next);
  };

  const navigateTabs = (
    event: KeyboardEvent<HTMLButtonElement>,
    current: RailTab,
  ): void => {
    const index = RAIL_TABS.indexOf(current);
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (index + 1) % RAIL_TABS.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (index - 1 + RAIL_TABS.length) % RAIL_TABS.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = RAIL_TABS.length - 1;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    const next = RAIL_TABS[nextIndex];
    selectRailTab(next);
    if (next === "chat") props.onChatSelected?.();
    tabRefs.current[next]?.focus();
  };

  const continueCothinkInChat = async (): Promise<void> => {
    selectRailTab("chat");
    props.onChatSelected?.();
  };

  return (
    <div className="wb-cowork-rail">
      {props.showTabs !== false ? <div
        className="wb-cowork-rail__tabs"
        role="tablist"
        aria-label="Co-work side panels"
      >
        <HelpTarget content={REVIEW_TAB_HELP} placement="bottom start">
          <button
            type="button"
            ref={(element) => {
              tabRefs.current.review = element;
            }}
            role="tab"
            id="wb-cowork-rail-tab-review"
            className="wb-cowork-rail__tab"
            aria-selected={tab === "review"}
            tabIndex={tab === "review" ? 0 : -1}
            aria-controls="wb-cowork-rail-panel-review"
            onClick={() => selectRailTab("review")}
            onKeyDown={(event) => navigateTabs(event, "review")}
          >
            Review
          </button>
        </HelpTarget>
        <HelpTarget content={TRUTH_TAB_HELP} placement="bottom">
          <button
            type="button"
            ref={(element) => {
              tabRefs.current.truth = element;
            }}
            role="tab"
            id="wb-cowork-rail-tab-truth"
            className="wb-cowork-rail__tab"
            aria-selected={tab === "truth"}
            tabIndex={tab === "truth" ? 0 : -1}
            aria-controls="wb-cowork-rail-panel-truth"
            onClick={() => selectRailTab("truth")}
            onKeyDown={(event) => navigateTabs(event, "truth")}
          >
            Truth
          </button>
        </HelpTarget>
        <HelpTarget content={CHAT_TAB_HELP} placement="bottom">
          <button
            type="button"
            ref={(element) => {
              tabRefs.current.chat = element;
            }}
            role="tab"
            id="wb-cowork-rail-tab-chat"
            className="wb-cowork-rail__tab"
            aria-selected={tab === "chat"}
            tabIndex={tab === "chat" ? 0 : -1}
            aria-controls="wb-cowork-rail-panel-chat"
            onClick={() => {
              selectRailTab("chat");
              props.onChatSelected?.();
            }}
            onKeyDown={(event) => navigateTabs(event, "chat")}
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
          scrollContainerRef={reviewActive ? props.reviewScrollRef : undefined}
          onScrollContainerWillDetach={props.onReviewScrollWillDetach}
          reviewAnchors={props.reviewAnchors}
          shortcutBindings={props.shortcutBindings}
          active={reviewActive}
          onDiscussCothink={continueCothinkInChat}
          onRecheckIntent={props.onRecheckIntent}
          truthAttention={
            props.truth === undefined ? undefined : (
              <TruthAttentionFeed
                provider={props.truth.provider}
                onOpenClaim={(claimId) => {
                  props.truth?.store.selectClaim(claimId);
                  selectRailTab("truth");
                }}
              />
            )
          }
        />
      </div>

      <div
        role="tabpanel"
        id="wb-cowork-rail-panel-truth"
        aria-labelledby={
          props.showTabs === false
            ? "wb-cowork-mobile-tab-truth"
            : "wb-cowork-rail-tab-truth"
        }
        className="wb-cowork-rail__tabpanel"
        hidden={tab !== "truth"}
      >
        {props.truth === undefined || props.storeId === undefined ? (
          <p className="wb-cowork-rail__empty">
            Truth is available for documents opened from a Co-work folder.
          </p>
        ) : (
          <TruthPanel
            provider={props.truth.provider}
            storeId={props.storeId}
            documentId={props.documentId}
            store={props.truth.store}
            storage={props.storage}
            editor={props.truth.editor}
            readOnly={props.truth.readOnly}
            active={tab === "truth" && props.truthVisible !== false}
            scroll={{
              scrollContainerRef: props.truthScrollRef,
              onScrollContainerWillDetach: props.onTruthScrollWillDetach,
            }}
          />
        )}
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
          <CoworkConversationGate
            chat={props.chat}
            execution={props.chatExecution}
          />
        ) : (
          <CoworkChatPanel
            provider={props.chat.provider}
            conversationId={props.chat.conversationId}
            annotations={props.chatAnnotations}
            onScrollToAnchor={props.onScrollToChatAnchor}
            onMessagesChange={setMessages}
            execution={props.chatExecution}
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
        )}
      </div>
    </div>
  );
}

export default CoworkRail;
