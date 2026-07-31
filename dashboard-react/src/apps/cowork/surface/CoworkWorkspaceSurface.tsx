import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
  type ReactNode,
} from "react";
import { Group, Panel, Separator } from "react-resizable-panels";

import { useOptionalDashboardEvents } from "../../../dashboard/events/DashboardEventProvider";
import {
  HelpTarget,
  useDashboardHelpEnabled,
  type HelpContent,
} from "../../../dashboard/help";
import {
  createHttpChatExecutionProfileProvider,
  createHttpChatProvider,
} from "../../../dashboard/conversations";
import {
  useChatExecutionProfile,
  type ChatExecutionSelectionCandidate,
  type ChatExecutionSelectionInput,
  type ChatExecutionSwitchConfirmation,
} from "../../../widget-library/chat";
import {
  coworkDocumentCanWriteBackSource,
  type CoworkDocumentSummary,
  type CoworkDriftState,
  type CoworkViewModel,
} from "../contracts";
import type { CoworkPasteProvenanceRecorder } from "../provenance";
import { CoworkHttpClient } from "../providers/CoworkHttpClient";
import type { CoworkSyncStatus } from "../persistence/CoworkYdocPersistence";
import type {
  CoworkMaterializationController,
  CoworkMaterializationState,
  CoworkMaterializeReceipt,
} from "../materialization/contracts";
import {
  CoworkBridgeEditor,
  useCoworkBridge,
} from "../bridge";
import {
  CoworkChatAnnotations,
  CoworkChatTargetingProvider,
  coworkConversationEndpoint,
  coworkConversationExecutionEndpoint,
  useDocumentConversationBinding,
  type CoworkDocumentConversationBindingClient,
  type CoworkDocumentAgentStatus,
  type FeedbackCapture,
  type ScrollAnchorTarget,
} from "../chat";
import {
  CoworkEditorPane,
  type CoworkScratchPromotionHandle,
} from "../editor/CoworkEditorPane";
import {
  isChatDraftDirty,
  loadChatDraft,
  loadRailTab,
  saveRailTab,
  useUnsavedWorkGuard,
} from "../guards";
import { useCoworkNavBinding } from "../keyboard";
import {
  CoworkRail,
  InMemoryReviewProvider,
  RailStore,
  createDemoChatProvider,
  isDirty,
  type CoworkRailChat,
  type VerificationRecheckIntent,
} from "../rail";
import {
  CoworkDocumentActionBar,
  CoworkDocumentActionDock,
  type CoworkAffirmVerifyRecheckTargetHandler,
  type CoworkInvitePerspectiveHandler,
  type CoworkRunVerifyHandler,
} from "../targets";
import {
  HttpCoworkVerifyClient,
  useCoworkVerifyExecution,
} from "../verify";
import {
  EDITOR_DEFAULT_SIZE,
  EDITOR_MIN_SIZE,
  EDITOR_PANEL_ID,
  RAIL_DEFAULT_SIZE,
  RAIL_MAX_SIZE,
  RAIL_MIN_SIZE,
  RAIL_PANEL_ID,
  useResizableRail,
} from "./useResizableRail";
import "./styles.css";

const DRIFT_LABEL: Record<string, string> = {
  clean: "In sync",
  drifted: "Drifted from file",
  missing: "File missing",
};

export const coworkExecutionSwitchConfirmation = (
  agentStatus: CoworkDocumentAgentStatus,
  { providerLabel, modelLabel }: ChatExecutionSelectionCandidate,
): ChatExecutionSwitchConfirmation | null =>
  agentStatus === "running"
    ? {
        title: `Switch to ${providerLabel} · ${modelLabel}?`,
        description:
          "This restarts the assistant with the new model. Your messages and draft stay here.",
        confirmLabel: "Switch",
      }
    : null;

/**
 * Hover-help for the three Co-work regions, surfaced when app-shell help mode is on. The
 * editor copy is the pane's own description, kept here as help rather than seeded into the
 * document where it would read as fabricated content.
 */
export const coworkEditorHelp = (
  document: Pick<CoworkDocumentSummary, "sourceWriteback"> | null = null,
): HelpContent => ({
  summary: "Write and edit your document here.",
  details:
    document?.sourceWriteback === "never"
      ? "Your work is saved safely in Co-work as you edit. Agent suggestions appear separately for review, and the file you imported remains unchanged."
      : "Your work is saved safely in this browser as you edit. Agent suggestions appear separately for review, and Save updates the Markdown file in your folder.",
});

const COWORK_HEALTH_HELP: HelpContent = {
  summary: "Document health at a glance.",
  details:
    "Names the open document, whether the editor has drifted from the file on disk, and how many proposals are still open for review.",
};

/**
 * The seed for the dev-only demo fixture (Ruling 1: demo is no longer a product surface). Its
 * prose carries the exact phrases the in-memory review fixture anchors its proposals and claim
 * to, so the fixture scene reads as one coherent document beside its review rail. It stays as
 * test infrastructure behind the import.meta.env.DEV gate and is tree-shaken from production.
 */
const DEMO_DOCUMENT_MARKDOWN = [
  "# Context bundle cache",
  "",
  "The cache keys on the active collector set, so a bundle is reused across invocations that share it. Keys on a digest of every collector output.",
  "",
  "We always rebuild the bundle when a reported change lands. Benchmarks on the reference machine show cold-start latency dropped from 1.8 s to 1.1 s after prewarming.",
  "",
].join("\n");

/** The unified health view both modes feed the strip, so it renders identically. */
interface CoworkHealthView {
  readonly title: string;
  readonly driftState: CoworkDriftState;
  readonly openProposalCount: number;
}

type CoworkWorkspacePane = "editor" | "review" | "chat";
const NARROW_WORKSPACE_QUERY = "(max-width: 760px)";

const useNarrowWorkspace = (): boolean => {
  const [narrow, setNarrow] = useState(
    () =>
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia(NARROW_WORKSPACE_QUERY).matches,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const media = window.matchMedia(NARROW_WORKSPACE_QUERY);
    const update = (): void => setNarrow(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);
  return narrow;
};

function CoworkPaneTabs({
  active,
  onChange,
  editorTabRef,
}: {
  readonly active: CoworkWorkspacePane;
  readonly onChange: (pane: CoworkWorkspacePane) => void;
  readonly editorTabRef?: MutableRefObject<HTMLButtonElement | null>;
}) {
  const panes: readonly CoworkWorkspacePane[] = ["editor", "review", "chat"];
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const select = (index: number): void => {
    const normalized = (index + panes.length) % panes.length;
    const pane = panes[normalized];
    onChange(pane);
    refs.current[normalized]?.focus();
  };
  return (
    <div className="wb-cowork__pane-tabs" role="tablist" aria-label="Co-work panes">
      {panes.map((pane, index) => (
        <button
          key={pane}
          ref={(element) => {
            refs.current[index] = element;
            if (pane === "editor" && editorTabRef !== undefined) {
              editorTabRef.current = element;
            }
          }}
          type="button"
          role="tab"
          id={`wb-cowork-mobile-tab-${pane}`}
          className="wb-cowork__pane-tab"
          aria-selected={active === pane}
          aria-controls={
            pane === "editor"
              ? "wb-cowork-mobile-panel-editor"
              : `wb-cowork-rail-panel-${pane}`
          }
          tabIndex={active === pane ? 0 : -1}
          onClick={() => onChange(pane)}
          onKeyDown={(event) => {
            if (event.key === "ArrowRight" || event.key === "ArrowDown") {
              event.preventDefault();
              select(index + 1);
            } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
              event.preventDefault();
              select(index - 1);
            } else if (event.key === "Home") {
              event.preventDefault();
              select(0);
            } else if (event.key === "End") {
              event.preventDefault();
              select(panes.length - 1);
            }
          }}
        >
          {pane[0].toLocaleUpperCase() + pane.slice(1)}
        </button>
      ))}
    </div>
  );
}

/**
 * Health strip region (`wb.widget-role.cowork-health-strip@1`). Read-only chrome:
 * document name, drift state, and open-proposal count. Drift is encoded with a text
 * label as well as a data attribute, so its meaning survives forced-colors (SP-6 G3).
 * In live mode the materialize confirmation reloads the review layer, which updates this
 * strip's drift and open-proposal count.
 */
function CoworkHealthStrip({ health }: { health: CoworkHealthView | null }) {
  return (
    <HelpTarget content={COWORK_HEALTH_HELP} placement="bottom start">
      <header className="wb-cowork__health" aria-label="Document health">
        <span className="wb-cowork__health-title">
          {health?.title ?? "Choose a document"}
        </span>
        {health !== null ? (
          <span className="wb-cowork__health-facts">
            <span className="wb-cowork__drift" data-drift={health.driftState}>
              {DRIFT_LABEL[health.driftState] ?? health.driftState}
            </span>
            <span className="wb-cowork__count">
              {health.openProposalCount} open proposal
              {health.openProposalCount === 1 ? "" : "s"}
            </span>
          </span>
        ) : null}
      </header>
    </HelpTarget>
  );
}

/**
 * The shared three-region shell, so demo and live compose the same layout (section 5). The
 * editor and the review rail are two resizable panels: react-resizable-panels sizes them as
 * percentages of the body, so the rail drags across a wide range in both directions and holds
 * its proportion when the window changes. The separator carries `role="separator"` with arrow
 * keys and double-click-to-reset from the library, and `useResizableRail` persists the split.
 *
 * The layout root is a plain `<div>`: the workspace card is one durable widget, and the grid
 * host renders the single `<main>` above it while the WidgetFrame provides the named region.
 * A second landmark here would nest inside that frame, so the shell owns styling only.
 */
function CoworkWorkspaceLayout({
  health,
  editorTopBar = null,
  workspaceBottomDock = null,
  editorHelp = coworkEditorHelp(),
  editor,
  rail,
  railRef,
  showHealth = true,
  narrow = false,
  activePane = "editor",
  paneTabs = null,
}: {
  readonly health: CoworkHealthView | null;
  readonly editorTopBar?: ReactNode;
  readonly workspaceBottomDock?: ReactNode;
  readonly editorHelp?: HelpContent;
  readonly editor: ReactNode;
  readonly rail: ReactNode;
  readonly railRef?: (element: HTMLElement | null) => void;
  readonly showHealth?: boolean;
  readonly narrow?: boolean;
  readonly activePane?: CoworkWorkspacePane;
  readonly paneTabs?: ReactNode;
}) {
  const helping = useDashboardHelpEnabled();
  const { defaultLayout, onLayoutChanged } = useResizableRail();
  return (
    <div className={`wb-cowork${helping ? " is-helping" : ""}`}>
      {showHealth ? <CoworkHealthStrip health={health} /> : null}
      {paneTabs}
      <Group
        className="wb-cowork__body"
        data-narrow={narrow}
        orientation="horizontal"
        defaultLayout={defaultLayout}
        onLayoutChanged={onLayoutChanged}
      >
        <Panel
          id={EDITOR_PANEL_ID}
          className="wb-cowork__editor-panel"
          defaultSize={EDITOR_DEFAULT_SIZE}
          minSize={EDITOR_MIN_SIZE}
          data-pane-active={!narrow || activePane === "editor"}
        >
          <div
            id="wb-cowork-mobile-panel-editor"
            className="wb-cowork__editor-shell"
            role={narrow ? "tabpanel" : undefined}
            aria-labelledby={narrow ? "wb-cowork-mobile-tab-editor" : undefined}
            hidden={narrow && activePane !== "editor"}
            inert={narrow && activePane !== "editor" ? true : undefined}
          >
            {editorTopBar}
            <HelpTarget content={editorHelp} placement="top">
              <div className="wb-cowork__editor-region">{editor}</div>
            </HelpTarget>
          </div>
        </Panel>
        <Separator
          className="wb-cowork__rail-separator"
          aria-label="Resize the review panel"
          hidden={narrow}
        />
        <Panel
          id={RAIL_PANEL_ID}
          className="wb-cowork__rail-panel"
          defaultSize={RAIL_DEFAULT_SIZE}
          minSize={RAIL_MIN_SIZE}
          maxSize={RAIL_MAX_SIZE}
          data-pane-active={!narrow || activePane !== "editor"}
        >
          <aside
            className="wb-cowork__rail"
            aria-label="Review and chat"
            ref={railRef}
            hidden={narrow && activePane === "editor"}
            inert={narrow && activePane === "editor" ? true : undefined}
          >
            {rail}
          </aside>
        </Panel>
      </Group>
      {workspaceBottomDock}
    </div>
  );
}

export const healthFromModel = (
  model: CoworkViewModel | null,
): CoworkHealthView | null => {
  const document = model?.document ?? null;
  if (document === null) return null;
  return {
    title: document.title,
    driftState: document.driftState,
    openProposalCount: document.openProposalCount,
  };
};

export const healthFromDocument = (
  document: CoworkDocumentSummary,
): CoworkHealthView => ({
  title: document.title,
  driftState: document.driftState,
  openProposalCount: document.openProposalCount,
});

/**
 * The dev-only demo fixture scene (Ruling 1: not a product surface). The in-memory review and
 * demo chat providers back the rail and the editor pane keys its local transport to the demo
 * document id, so widget-lab, the tests, and the dev-server e2e suites render the same
 * deterministic scene with no network. The whole composition sits behind import.meta.env.DEV
 * at its call site, so production tree-shakes it entirely.
 */
export function CoworkDemoWorkspace({
  model,
}: {
  readonly model: CoworkViewModel | null;
}) {
  const documentId = model?.document?.documentId ?? "demo-doc";
  const conversationId = `cowork-doc-${documentId}`;
  const reviewProvider = useMemo(() => new InMemoryReviewProvider(), []);
  const chatProvider = useMemo(
    () => createDemoChatProvider(conversationId),
    [conversationId],
  );

  return (
    <CoworkWorkspaceLayout
      health={healthFromModel(model)}
      editor={
        <CoworkEditorPane documentId={documentId} seedMarkdown={DEMO_DOCUMENT_MARKDOWN} />
      }
      rail={
        <CoworkRail
          documentId={documentId}
          reviewProvider={reviewProvider}
          chat={{
            kind: "ready",
            provider: chatProvider,
            conversationId,
            draftStorageId: conversationId,
            agent: {
              status: "running",
              alive: true,
              started: true,
              error: null,
            },
            onEnsureAgent: () => {},
          }}
        />
      }
    />
  );
}

/**
 * Compatibility empty fixture. The production widget supplies the actionable lifecycle
 * launcher; this boundary intentionally mounts no editor, rail, chat, or resizer.
 */
/** A crash-safe untitled document kept on this device until the user saves it to a folder. */
export function CoworkScratchWorkspace({
  scratchId,
  onPromotionHandle,
  onSyncStatus,
  onLocalEdit,
}: {
  readonly scratchId: string;
  readonly onPromotionHandle?: (handle: CoworkScratchPromotionHandle | null) => void;
  readonly onSyncStatus?: (status: CoworkSyncStatus) => void;
  readonly onLocalEdit?: () => void;
}) {
  return (
    <div className="wb-cowork wb-cowork--local-document">
      <div className="wb-cowork__editor-region">
        <CoworkEditorPane
          documentId={scratchId}
          onPromotionHandle={onPromotionHandle}
          onSyncStatus={onSyncStatus}
          onLocalEdit={onLocalEdit}
        />
      </div>
    </div>
  );
}

/**
 * Live mode (the default on a ledger-backed folder). The bridge shares one canonical Y.Doc
 * and one R2 pull across the editor and rail, so cards and view-only decorations agree. A
 * doc-scoped SSE nudge reloads the review layer, and the aligned stream measures generalized
 * decoration anchors through the anchor-rect source.
 */
export function CoworkLiveWorkspace({
  documentId,
  storeId,
  document,
  fallbackHealth,
  showHealth = true,
  readOnly = false,
  feedbackCapture = true,
  onSyncStatus,
  onMaterializationState,
  onMaterializationController,
  onMaterialized,
  conversationBindingClient,
  pasteProvenanceRecorder,
  onRunVerify,
  onAffirmRecheckTarget,
}: {
  readonly documentId: string;
  readonly storeId: string;
  readonly document: CoworkDocumentSummary;
  readonly fallbackHealth: CoworkHealthView | null;
  readonly showHealth?: boolean;
  readonly readOnly?: boolean;
  readonly feedbackCapture?: boolean;
  readonly onSyncStatus?: (status: CoworkSyncStatus) => void;
  readonly onMaterializationState?: (state: CoworkMaterializationState) => void;
  readonly onMaterializationController?: (
    controller: CoworkMaterializationController | null,
  ) => void;
  readonly onMaterialized?: (receipt: CoworkMaterializeReceipt) => void;
  /** Injectable server-binding client for focused integration tests. */
  readonly conversationBindingClient?: CoworkDocumentConversationBindingClient;
  /** Injectable exact-span paste recorder; live mode uses the same-origin API. */
  readonly pasteProvenanceRecorder?: CoworkPasteProvenanceRecorder;
  /** Exact capture handoff; route transport remains injectable at this boundary. */
  readonly onRunVerify?: CoworkRunVerifyHandler;
  readonly onAffirmRecheckTarget?: CoworkAffirmVerifyRecheckTargetHandler;
  readonly onInvitePerspective?: CoworkInvitePerspectiveHandler;
}) {
  const workspaceIdentity = `${storeId}\u0000${documentId}`;
  const workspaceIdentityRef = useRef(workspaceIdentity);
  workspaceIdentityRef.current = workspaceIdentity;

  // One document conversation linkage store per document. The submit path annotates a routing
  // note delivery here, and the feedback entry point annotates the captured span when R9 lands.
  const annotations = useMemo(
    () => new CoworkChatAnnotations(),
    [documentId, storeId],
  );

  // The rail store is owned here so the route-change guard reads the same staged sitting the
  // rail mutates, and the review keyboard binding comes from the settings registry. The tab
  // seeds from and mirrors back to localStorage, so a reload keeps the Review or Chat choice
  // (the onFeedbackCaptured switch to Chat below now persists through the same seam).
  const [railStore] = useState(
    () =>
      new RailStore(
        { tab: loadRailTab(window.localStorage, documentId) ?? "review" },
        { onTabChange: (tab) => saveRailTab(window.localStorage, documentId, tab) },
      ),
  );
  const conversation = useDocumentConversationBinding({
    documentId,
    storeId,
    client: conversationBindingClient,
  });
  useEffect(() => {
    annotations.replaceFeedback(conversation.feedback);
  }, [annotations, conversation.feedback]);
  const adoptExecutionRef = useRef(conversation.adoptExecution);
  adoptExecutionRef.current = conversation.adoptExecution;
  const executionProvider = useMemo(
    () => {
      const providerWorkspaceIdentity = workspaceIdentity;
      return createHttpChatExecutionProfileProvider({
        targetId: workspaceIdentity,
        loadUrl: coworkConversationEndpoint(documentId, storeId),
        selectUrl: coworkConversationExecutionEndpoint(documentId, storeId),
        onEnvelope: (envelope) => {
          if (
            workspaceIdentityRef.current !== providerWorkspaceIdentity
          ) {
            return;
          }
          adoptExecutionRef.current(
            documentId,
            storeId,
            envelope.execution,
            envelope.agent,
          );
        },
      });
    },
    [documentId, storeId, workspaceIdentity],
  );
  useEffect(() => {
    if (conversation.execution !== undefined) {
      executionProvider.replaceSnapshot(conversation.execution);
    }
  }, [conversation.execution, executionProvider]);
  const chatExecution = useChatExecutionProfile(
    conversation.execution === undefined ? null : executionProvider,
    workspaceIdentity,
  );
  const presentedChatExecution = useMemo(() => {
    if (chatExecution === undefined) return undefined;
    return {
      ...chatExecution,
      confirmSelection: (candidate: ChatExecutionSelectionCandidate) =>
        coworkExecutionSwitchConfirmation(
          conversation.agent.status,
          candidate,
        ),
    };
  }, [chatExecution, conversation.agent.status]);
  const verifyExecution = useCoworkVerifyExecution(
    chatExecution,
    workspaceIdentity,
  );
  const chatDraftStorageId = `document:${storeId}:${documentId}`;
  const chatProvider = useMemo(
    () =>
      conversation.phase === "ready" && conversation.conversationId !== null
        ? createHttpChatProvider({
            conversationId: conversation.conversationId,
          })
        : null,
    [conversation.conversationId, conversation.phase],
  );
  const selectedExecution: ChatExecutionSelectionInput | undefined =
    chatExecution?.snapshot === null || chatExecution?.snapshot === undefined
      ? undefined
      : {
          providerId: chatExecution.snapshot.selection.providerId,
          modelId: chatExecution.snapshot.selection.modelId,
          expectedRevision: chatExecution.snapshot.selection.revision,
        };
  const verifyClient = useMemo(
    () => new HttpCoworkVerifyClient({ documentId, storeId }),
    [documentId, storeId],
  );
  const ensureConversation = useCallback(
    (): Promise<void> => conversation.ensure(selectedExecution),
    [conversation.ensure, selectedExecution],
  );
  const chat: CoworkRailChat = useMemo(() => {
    if (
      conversation.phase === "ready" &&
      conversation.conversationId !== null &&
      chatProvider !== null
    ) {
      return {
        kind: "ready",
        provider: chatProvider,
        conversationId: conversation.conversationId,
        draftStorageId: chatDraftStorageId,
        agent: conversation.agent,
        ensuringAgent: conversation.ensuring,
        ensureError: conversation.error,
        onEnsureAgent: ensureConversation,
      };
    }
    if (conversation.phase === "idle") {
      return {
        kind: "idle",
        draftStorageId: chatDraftStorageId,
        onStart: ensureConversation,
      };
    }
    if (conversation.phase === "error") {
      return {
        kind: "error",
        draftStorageId: chatDraftStorageId,
        error:
          conversation.error ?? "Chat could not be loaded.",
        action:
          conversation.conversationId === null ? "start" : "restart",
        onRetry: ensureConversation,
      };
    }
    return {
      kind: conversation.phase === "ensuring" ? "ensuring" : "loading",
      draftStorageId: chatDraftStorageId,
    };
  }, [
    chatDraftStorageId,
    chatProvider,
    conversation.agent,
    conversation.conversationId,
    conversation.error,
    conversation.ensuring,
    conversation.phase,
    ensureConversation,
  ]);
  const narrowWorkspace = useNarrowWorkspace();
  const [activePane, setActivePane] = useState<CoworkWorkspacePane>("editor");
  const editorPaneTabRef = useRef<HTMLButtonElement | null>(null);
  const [passageAnnouncement, setPassageAnnouncement] = useState("");
  const [armedVerifyRecheck, setArmedVerifyRecheck] =
    useState<VerificationRecheckIntent | null>(null);
  const selectPane = useCallback(
    (pane: CoworkWorkspacePane): void => {
      setActivePane(pane);
      if (pane !== "editor") railStore.setTab(pane);
    },
    [railStore],
  );
  useEffect(
    () =>
      railStore.subscribe(() => {
        if (narrowWorkspace) setActivePane(railStore.getState().tab);
      }),
    [narrowWorkspace, railStore],
  );
  useEffect(() => setActivePane("editor"), [documentId]);
  useEffect(() => {
    setArmedVerifyRecheck(null);
  }, [documentId, storeId]);
  const navBinding = useCoworkNavBinding();
  const provenanceClient = useMemo(() => new CoworkHttpClient(), []);
  const defaultPasteProvenanceRecorder =
    useCallback<CoworkPasteProvenanceRecorder>(
      (request) => provenanceClient.recordPasteProvenance(request).then(() => undefined),
      [provenanceClient],
    );

  const bridge = useCoworkBridge({
    documentId,
    storeId,
    readOnly,
    onSyncStatus,
    currentFileSha256: document.currentFileSha256,
    initialDriftState: document.driftState,
    canMaterialize: coworkDocumentCanWriteBackSource(document),
    onMaterializationState,
    onMaterializationController,
    onMaterialized,
    pasteProvenanceRecorder:
      pasteProvenanceRecorder ?? defaultPasteProvenanceRecorder,
    onRoutingDelivery: (delivery) => {
      annotations.annotateRoutingDelivery(delivery);
      if (
        delivery.conversationId !== undefined &&
        delivery.execution !== undefined
      ) {
        conversation.adoptExecution(
          documentId,
          storeId,
          delivery.execution,
          delivery.agent,
          delivery.conversationId,
        );
      }
    },
    // The last link of the feedback loop: R9 landed, so record the span-linked message on
    // the Chat tab and switch the rail to Chat so the human sees the feedback land.
    ...(feedbackCapture
      ? {
          onFeedbackCaptured: (capture: FeedbackCapture) => {
            if (
              workspaceIdentityRef.current !==
              `${capture.storeId}\u0000${capture.documentId}`
            ) {
              return;
            }
            conversation.adoptFeedback(capture);
            annotations.annotateFeedback(capture);
            railStore.setTab("chat");
          },
        }
      : {}),
  });
  const resolvedRunVerify = useMemo<CoworkRunVerifyHandler | undefined>(() => {
    if (onRunVerify !== undefined) return onRunVerify;
    return async (capture, intent) => {
      await verifyClient.startVerify(capture, intent.execution, {
        userGoal: intent.userGoal,
        protectedIntent: intent.protectedIntent,
        recheckOfProposalIds: intent.recheck?.pendingProposalIds,
        recheckOfRunId: intent.recheck?.sourceRunId,
        recheckIntentId: intent.recheck?.intentId,
        recheckTargetConfirmation: intent.recheck?.targetConfirmation,
      });
      bridge.reviewProvider.invalidate();
    };
  }, [
    bridge.reviewProvider,
    onRunVerify,
    verifyClient,
  ]);
  const resolvedAffirmRecheckTarget =
    useMemo<CoworkAffirmVerifyRecheckTargetHandler>(() => {
      if (onAffirmRecheckTarget !== undefined) {
        return onAffirmRecheckTarget;
      }
      return async (capture, intent) =>
        verifyClient.affirmRecheckTarget(capture, intent);
    }, [onAffirmRecheckTarget, verifyClient]);
  const recheckIntent = useCallback(
    (request: VerificationRecheckIntent): Promise<void> => {
      if (request.status === "fulfilled") return Promise.resolve();
      setArmedVerifyRecheck(request);
      setPassageAnnouncement(
        request.status === "user_action_required"
          ? "Bound recheck opened in Verify. Set and affirm Working on before running it."
          : "Bound recheck opened in Verify with its original target. Review it, then run Verify.",
      );
      if (narrowWorkspace) {
        selectPane("editor");
        window.requestAnimationFrame(() => editorPaneTabRef.current?.focus());
      }
      return Promise.resolve();
    },
    [narrowWorkspace, selectPane],
  );
  const scrollToChatAnchor = useCallback(
    (target: ScrollAnchorTarget): void => {
      const announceResult = (found: boolean): void => {
        setPassageAnnouncement(
          found
            ? "Passage highlighted in editor."
            : "That passage could not be found.",
        );
      };
      if (!narrowWorkspace) {
        announceResult(bridge.scrollToSpanAnchor(target));
        return;
      }
      // The editor is hidden while Chat owns the narrow workspace. Reveal it first so the
      // bridge can scroll the rendered passage decoration instead of a hidden ProseMirror DOM.
      // Move focus to the visible pane tab too: the triggering Chat control becomes inert.
      setActivePane("editor");
      window.requestAnimationFrame(() => {
        editorPaneTabRef.current?.focus();
        announceResult(bridge.scrollToSpanAnchor(target));
      });
    },
    [bridge.scrollToSpanAnchor, narrowWorkspace],
  );

  // The union route-change guard (guards/routeGuard): a staged-but-unsubmitted sitting or an
  // unsent chat draft warns before a browser-level navigation. Read at event time, so it sees
  // the live sitting and the retained draft.
  const guardDirty = useCallback(
    () =>
      isDirty(railStore.getState()) ||
      isChatDraftDirty(
        loadChatDraft(window.localStorage, chatDraftStorageId) ?? "",
      ),
    [railStore, chatDraftStorageId],
  );
  useUnsavedWorkGuard(guardDirty);

  // The SSE nudge (section 1.11): a truth.doc_* event reloads the review layer, which
  // re-pulls R2 and reconciles the cards, the marks, and the health strip.
  const events = useOptionalDashboardEvents();
  const invalidationSequence = events?.lastInvalidation?.sequence;
  const invalidationReason = events?.lastInvalidation?.invalidation.reason;
  useEffect(() => {
    if (invalidationReason?.startsWith("truth.doc_") === true) {
      bridge.reviewProvider.invalidate();
    }
    // Fire once per new invalidation, keyed by its sequence.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invalidationSequence]);

  const health: CoworkHealthView | null =
    bridge.health === null
      ? fallbackHealth
      : {
          title: bridge.health.title,
          driftState: bridge.health.drift.state,
          openProposalCount: bridge.health.drift.openProposalCount,
        };

  return (
    <CoworkWorkspaceLayout
      health={health}
      editorHelp={coworkEditorHelp(document)}
      showHealth={showHealth}
      editorTopBar={
        <CoworkDocumentActionBar
          controller={bridge.actionSnapshotController}
        />
      }
      workspaceBottomDock={
        <CoworkDocumentActionDock
          storeId={storeId}
          documentId={documentId}
          controller={bridge.actionSnapshotController}
          reviewProvider={bridge.reviewProvider}
          readOnly={readOnly}
          onRunVerify={resolvedRunVerify}
          onAffirmRecheckTarget={resolvedAffirmRecheckTarget}
          verifySetup={bridge.verifySetup}
          verifyCapability={bridge.verifyCapability}
          execution={verifyExecution}
          armedRecheck={armedVerifyRecheck}
          onClearArmedRecheck={() => setArmedVerifyRecheck(null)}
        />
      }
      railRef={bridge.railRef}
      narrow={narrowWorkspace}
      activePane={activePane}
      paneTabs={
        <>
          {narrowWorkspace ? (
            <CoworkPaneTabs
              active={activePane}
              onChange={selectPane}
              editorTabRef={editorPaneTabRef}
            />
          ) : null}
          <p
            className="wb-visually-hidden"
            role="status"
            aria-live="polite"
          >
            {passageAnnouncement}
          </p>
        </>
      }
      editor={<CoworkBridgeEditor {...bridge.editorProps} />}
      rail={
        <CoworkChatTargetingProvider
          storeId={storeId}
          documentId={documentId}
          controller={bridge.actionSnapshotController}
          agent={conversation.agent}
        >
          <CoworkRail
            documentId={documentId}
            reviewProvider={bridge.reviewProvider}
            chat={chat}
            anchorRects={bridge.anchorRects}
            store={railStore}
            queueBindings={navBinding}
            chatAnnotations={annotations}
            chatExecution={presentedChatExecution}
            onScrollToChatAnchor={scrollToChatAnchor}
            narrow={narrowWorkspace}
            reviewVisible={
              !narrowWorkspace || activePane === "review"
            }
            showTabs={!narrowWorkspace}
            onRecheckIntent={recheckIntent}
          />
        </CoworkChatTargetingProvider>
      }
    />
  );
}

export type CoworkFixtureMode = "demo" | "live" | "empty";

/**
 * Decide empty vs live, with a dev-only demo fixture entry. The honest default is empty (no
 * document, honest empty states). A live scope with a resolvable store id and document id is
 * live, supplied on navigation as the same `store_id` the routes take. The demo scene is not a
 * product surface (Ruling 1): `?cowork_fixture=demo` resolves to it only when
 * import.meta.env.DEV is true, which is the dev server the e2e suites drive. In a production
 * build import.meta.env.DEV is statically false, so the demo branch and every
 * CoworkDemoWorkspace it selects are tree-shaken out, leaving the honest empty default and a
 * live store-scoped session as the only production modes.
 */
export function resolveFixtureMode(
  quality: string | undefined,
  documentId: string | undefined,
  storeId: string | undefined,
  override: string | null,
): CoworkFixtureMode {
  if (import.meta.env.DEV && override === "demo") return "demo";
  const wantLive = override === "live" || quality !== "demo";
  if (wantLive && documentId !== undefined && storeId !== undefined) return "live";
  return "empty";
}
