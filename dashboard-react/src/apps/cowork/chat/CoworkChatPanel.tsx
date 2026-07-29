// The thin Co-work adapter around the dashboard's reusable ConversationChat.
// It maps document-specific feedback, routing, and agent lifecycle state into
// the shared surface's narrow extension seams. Message structure, unread state,
// loading, retries, questions, and composer behavior remain shared.

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";

import {
  ConversationChat,
  type ChatConversationProvider,
  type ChatExecutionControl,
  type ChatInputRecovery,
  type ChatMessage,
  type ChatSendInput,
  type ConversationChatState,
} from "../../../widget-library/chat";
import { SwitchField } from "../../../ui";
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
import { useOptionalCoworkChatTargeting } from "./CoworkChatTargeting";
import type { ScrollAnchorTarget } from "./contracts";
import type { CoworkDocumentAgent } from "./documentConversationBinding";
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
  /** Server-owned state of the document agent bound to this conversation. */
  readonly agent?: CoworkDocumentAgent;
  /** A start or restart request is in flight. */
  readonly ensuringAgent?: boolean;
  /** A failed start or restart request. */
  readonly ensureError?: string | null;
  /** Present-user-intent start or restart action. */
  readonly onEnsureAgent?: () => void;
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
  const [attachWorkingTarget, setAttachWorkingTarget] = useState(false);
  useEffect(() => {
    setAttachWorkingTarget(false);
  }, [targeting?.documentId, targeting?.storeId]);

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
      // Working-on choice applies only to ordinary authored composer turns.
      if (
        !attachWorkingTarget ||
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
      const capture = await targeting.controller.capture("working_target");
      const context = await targeting.client.prepare(capture);
      return { ...input, context };
    },
    [attachWorkingTarget, targetUnavailableReason, targeting],
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

  const resolveInputRecovery = useCallback(
    (state: ConversationChatState): ChatInputRecovery | undefined => {
      const startFailed = agent?.status === "spawn_failed";
      const notStarted = agent?.status === "not_started";
      const stopped =
        agent?.status === "stopped" ||
        (!startFailed &&
          !notStarted &&
          state.agentActivity === "stopped");
      if (!startFailed && !notStarted && !stopped) return undefined;

      const titleText = ensuringAgent
        ? startFailed
          ? "Trying again…"
          : notStarted
            ? "Starting chat…"
            : "Restarting chat…"
        : startFailed
          ? "Chat couldn’t start."
          : notStarted
            ? "Chat hasn’t started."
            : "Chat paused.";
      const rawError = ensureError ?? agent?.error;
      const detail =
        ensuringAgent
          ? "Your messages are still here."
          : rawError === "Chat couldn’t start. Try again."
            ? "Try again."
            : rawError ??
              (startFailed
                ? "Try again."
                : notStarted
                  ? "Start chat to ask about this document."
                  : "Your messages are still here.");
      const actionLabel = ensuringAgent
        ? startFailed
          ? "Trying again…"
          : notStarted
            ? "Starting…"
            : "Restarting…"
        : startFailed
          ? "Try again"
          : notStarted
            ? "Start chat"
            : "Restart chat";

      return {
        tone: stopped || startFailed ? "warning" : "info",
        title: titleText,
        detail,
        preserveComposer: true,
        ...(onEnsureAgent === undefined
          ? {}
          : {
              action: {
                label: actionLabel,
                onAction: onEnsureAgent,
                pending: ensuringAgent,
                requiresExecution: true,
              },
            }),
      };
    },
    [
      agent?.error,
      agent?.status,
      ensureError,
      ensuringAgent,
      onEnsureAgent,
    ],
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
      composerAccessory={
        targeting === null ? null : (
          <div className="wb-cowork-chat-target">
            <SwitchField
              label="Use Working on for this message"
              selected={attachWorkingTarget}
              disabled={targetUnavailableReason !== null}
              onChange={setAttachWorkingTarget}
            />
            <span className="wb-cowork-chat-target__summary">
              {targetState.workingTarget.label}
              {targetState.phase === "ready" &&
              targetState.workingTarget.kind !== "unresolved"
                ? ` · ${targetState.workingTarget.wordCount.toLocaleString()} words`
                : ""}
            </span>
            {targetUnavailableReason === null ? (
              <span className="wb-cowork-chat-target__hint">
                Captures an exact frozen version when you send.
                {targeting.agent.status !== "running" ||
                targeting.agent.alive === false
                  ? " Your message will stay here until Chat restarts."
                  : ""}
              </span>
            ) : (
              <span
                className="wb-cowork-chat-target__unavailable"
                role="status"
              >
                {targetUnavailableReason}
              </span>
            )}
          </div>
        )
      }
      transcriptAppendix={
        <CoworkRoutingNotices
          deliveries={linkage.routing}
          onDismiss={(id) => store.dismissRoutingDelivery(id)}
        />
      }
      inputRecovery={resolveInputRecovery}
      readOnlyReason="This chat is closed."
      execution={execution}
    />
  );
}

export default CoworkChatPanel;
