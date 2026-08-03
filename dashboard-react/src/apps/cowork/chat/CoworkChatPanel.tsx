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
  HelpTarget,
  type HelpContent,
} from "../../../dashboard/help";
import {
  ConversationChat,
  type ChatConversationProvider,
  type ChatExecutionControl,
  type ChatMessage,
  type ChatSendInput,
} from "../../../widget-library/chat";
import type { CoworkActionSnapshotControllerState } from "../targets";
import {
  CoworkChatAnnotations,
  resolveSpanLink,
} from "./annotations";
import {
  CoworkActionSnapshotProvenance,
  CoworkPassageAction,
  CoworkRoutingNotices,
} from "./CoworkChatExtensions";
import { CoworkChatActionSnapshotError } from "./HttpCoworkChatActionSnapshotClient";
import { useOptionalCoworkChatTargeting } from "./CoworkChatTargeting";
import type { ScrollAnchorTarget } from "./contracts";
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
  /** Let the owning rail derive unread state without mounting another hook. */
  readonly onMessagesChange?: (messages: readonly ChatMessage[]) => void;
  /** Generic provider/model selection owned by the shared Chat surface. */
  readonly execution?: ChatExecutionControl;
}

const TARGET_LOADING_STATE: CoworkActionSnapshotControllerState = {
  phase: "loading",
  selection: null,
  currentSection: null,
  workingTarget: {
    kind: "document",
    label: "Whole document",
    wordCount: 0,
    range: null,
  },
};
const subscribeWithoutTarget = (): (() => void) => () => undefined;
const getTargetLoadingState = (): CoworkActionSnapshotControllerState =>
  TARGET_LOADING_STATE;

const CHAT_TARGET_HELP: HelpContent = {
  summary: "Sets what this message is about.",
  details:
    "Chat uses the editor’s shared Working on target and captures its exact document version when you send. Change Working on above the editor to change the target; later edits do not rewrite a sent message’s context.",
};

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
  onMessagesChange,
  execution,
}: CoworkChatPanelProps) {
  const store = useMemo(
    () => annotations ?? new CoworkChatAnnotations(),
    [annotations],
  );
  const linkage = useSyncExternalStore(store.subscribe, store.getSnapshot);
  const targeting = useOptionalCoworkChatTargeting();
  const targetState = useSyncExternalStore(
    targeting?.controller?.subscribe ?? subscribeWithoutTarget,
    targeting?.controller?.getSnapshot ?? getTargetLoadingState,
    getTargetLoadingState,
  );

  const targetUnavailableReason = useMemo(() => {
    if (targeting === null) return null;
    if (targeting.controller === null || targetState.phase !== "ready") {
      return "Document context is still loading.";
    }
    if (targetState.workingTarget.kind === "unresolved") {
      return "Working on needs attention in the editor before Chat can use it.";
    }
    return null;
  }, [targetState, targeting]);

  const prepareSend = useCallback(
    async (input: ChatSendInput): Promise<ChatSendInput> => {
      // Structured answers remain exact question responses. The explicit
      // Working on target applies only to ordinary authored composer turns.
      if (
        input.inReplyTo !== undefined ||
        targeting === null
      ) {
        return input;
      }
      if (
        targetUnavailableReason !== null ||
        targeting.controller === null
      ) {
        throw new Error(
          targetUnavailableReason ??
            "Working-on context is unavailable for this Chat.",
        );
      }
      const firstCapture = await targeting.controller.capture("working_target");
      try {
        const context = await targeting.client.prepare(firstCapture);
        return { ...input, context };
      } catch (error) {
        if (
          !(error instanceof CoworkChatActionSnapshotError) ||
          error.code !== "action_snapshot_changed"
        ) {
          throw error;
        }
        // A collaborator can advance the durable head after the browser's
        // stable capture but before its POST arrives. Recapture exactly once
        // against the first capture's logical target; a newer Working on
        // choice must never silently retarget the already-authored message.
        const reference = firstCapture.target.targetReference;
        if (
          targeting.controller.captureReference === undefined ||
          (firstCapture.target.selector.kind !== "document" &&
            reference === undefined)
        ) {
          throw error;
        }
        const context = await targeting.client.prepare(
          await targeting.controller.captureReference(
            "working_target",
            reference ?? null,
          ),
        );
        return { ...input, context };
      }
    },
    [targetUnavailableReason, targeting],
  );

  const renderMessageAccessory = useCallback(
    (message: ChatMessage) => {
      const link =
        onScrollToAnchor === undefined
          ? null
          : resolveSpanLink(message, linkage.feedback);
      if (link === null && message.context === undefined) return null;
      return (
        <>
          {message.context === undefined ? null : (
            <CoworkActionSnapshotProvenance
              context={message.context}
              author={message.author}
            />
          )}
          {link === null || onScrollToAnchor === undefined ? null : (
            <CoworkPassageAction
              link={link}
              onActivate={onScrollToAnchor}
            />
          )}
        </>
      );
    },
    [linkage.feedback, onScrollToAnchor],
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
      prepareSend={targeting === null ? undefined : prepareSend}
      composerFooterAccessory={
        targeting === null ? null : (
          <HelpTarget
            content={CHAT_TARGET_HELP}
            placement="top start"
            focusable
          >
            <span
              className={`wb-chat-composer__footer-accessory wb-cowork-chat-target${
                targetUnavailableReason === null ? "" : " is-unavailable"
              }`}
              aria-label={
                targetUnavailableReason === null
                  ? `About: ${targetState.workingTarget.label}. An exact version will be captured when sent.`
                  : `Message target unavailable. ${targetUnavailableReason}`
              }
              role={targetUnavailableReason === null ? undefined : "status"}
            >
              <span className="wb-cowork-chat-target__prefix">About:</span>{" "}
              <span className="wb-cowork-chat-target__label">
                {targetUnavailableReason === null
                  ? targetState.workingTarget.label
                  : "target unavailable"}
              </span>
              {targetUnavailableReason === null &&
              targetState.phase === "ready" &&
              targetState.workingTarget.kind !== "unresolved"
                ? ` · ${targetState.workingTarget.wordCount.toLocaleString()} words`
                : ""}
            </span>
          </HelpTarget>
        )
      }
      transcriptAppendix={
        <CoworkRoutingNotices
          deliveries={linkage.routing}
          onDismiss={(id) => store.dismissRoutingDelivery(id)}
        />
      }
      readOnlyReason="This chat is closed."
      execution={execution}
    />
  );
}

export default CoworkChatPanel;
