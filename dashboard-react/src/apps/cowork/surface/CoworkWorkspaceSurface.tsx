import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Group, Panel, Separator } from "react-resizable-panels";

import { useOptionalDashboardEvents } from "../../../dashboard/events/DashboardEventProvider";
import {
  HelpTarget,
  useDashboardHelpEnabled,
  type HelpContent,
} from "../../../dashboard/help";
import { InMemoryChatProvider } from "../../../widget-library/chat";
import type { CoworkDriftState, CoworkViewModel } from "../contracts";
import { CoworkBridgeEditor, useCoworkBridge } from "../bridge";
import { CoworkChatAnnotations } from "../chat";
import { CoworkEditorPane } from "../editor/CoworkEditorPane";
import {
  isChatDraftDirty,
  loadChatDraft,
  useUnsavedWorkGuard,
} from "../guards";
import { useCoworkNavBinding } from "../keyboard";
import {
  CoworkRail,
  InMemoryReviewProvider,
  RailStore,
  createDemoChatProvider,
  isDirty,
  type ReviewRailData,
} from "../rail";
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

/**
 * Hover-help for the three Co-work regions, surfaced when app-shell help mode is on. The
 * editor copy is the pane's own description, kept here as help rather than seeded into the
 * document where it would read as fabricated content.
 */
const COWORK_EDITOR_HELP: HelpContent = {
  summary: "This is the editor pane.",
  details:
    "It binds a Tiptap editor to a local Y.Doc through the eight-point load-order contract, projects AI proposals as an ephemeral review layer, and materializes edits block by block.",
};

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

const EMPTY_DOCUMENT_ID = "cowork-empty";
const EMPTY_CONVERSATION_ID = "cowork-doc-none";

/** The honest empty review layer: a titled-but-empty document with no proposals or claims. */
const EMPTY_REVIEW_DATA: ReviewRailData = {
  documentId: EMPTY_DOCUMENT_ID,
  title: "No document open",
  drift: {
    state: "clean",
    openProposalCount: 0,
    openFlagCount: 0,
    lastMaterializedSha256: null,
    currentFileSha256: null,
  },
  proposals: [],
  expressions: [],
  provenanceSpans: [],
  claims: [],
};

/** The unified health view both modes feed the strip, so it renders identically. */
interface CoworkHealthView {
  readonly title: string;
  readonly driftState: CoworkDriftState;
  readonly openProposalCount: number;
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
          {health?.title ?? "No document open"}
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
  editor,
  rail,
  railRef,
}: {
  readonly health: CoworkHealthView | null;
  readonly editor: ReactNode;
  readonly rail: ReactNode;
  readonly railRef?: (element: HTMLElement | null) => void;
}) {
  const helping = useDashboardHelpEnabled();
  const { defaultLayout, onLayoutChanged } = useResizableRail();
  return (
    <div className={`wb-cowork${helping ? " is-helping" : ""}`}>
      <CoworkHealthStrip health={health} />
      <Group
        className="wb-cowork__body"
        orientation="horizontal"
        defaultLayout={defaultLayout}
        onLayoutChanged={onLayoutChanged}
      >
        <Panel
          id={EDITOR_PANEL_ID}
          className="wb-cowork__editor-panel"
          defaultSize={EDITOR_DEFAULT_SIZE}
          minSize={EDITOR_MIN_SIZE}
        >
          <HelpTarget content={COWORK_EDITOR_HELP} placement="top">
            <div className="wb-cowork__editor-region">{editor}</div>
          </HelpTarget>
        </Panel>
        <Separator
          className="wb-cowork__rail-separator"
          aria-label="Resize the review panel"
        />
        <Panel
          id={RAIL_PANEL_ID}
          className="wb-cowork__rail-panel"
          defaultSize={RAIL_DEFAULT_SIZE}
          minSize={RAIL_MIN_SIZE}
          maxSize={RAIL_MAX_SIZE}
        >
          <aside className="wb-cowork__rail" aria-label="Review and chat" ref={railRef}>
            {rail}
          </aside>
        </Panel>
      </Group>
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
          chatProvider={chatProvider}
          conversationId={conversationId}
        />
      }
    />
  );
}

/**
 * Empty mode (the honest default). No document is open, so the health strip shows its
 * "No document open" state, the editor opens on an empty editable surface, and the rail
 * carries no fabricated proposals and no scripted agent turn: an empty review layer and an
 * empty document conversation with a real composer.
 */
export function CoworkEmptyWorkspace() {
  const reviewProvider = useMemo(
    () => new InMemoryReviewProvider({ data: EMPTY_REVIEW_DATA }),
    [],
  );
  const chatProvider = useMemo(
    () =>
      new InMemoryChatProvider({
        conversationId: EMPTY_CONVERSATION_ID,
        title: "Document conversation",
        status: "open",
        agentLiveness: "unknown",
        messages: [],
      }),
    [],
  );

  return (
    <CoworkWorkspaceLayout
      health={null}
      // Thread the stable empty-document id so the editor keys its local, reload-surviving
      // transport to this scratch document (the coordination seam with the persistence work).
      // The prop is optional, so this compiles whether or not the pane consumes it yet.
      editor={<CoworkEditorPane documentId={EMPTY_DOCUMENT_ID} />}
      rail={
        <CoworkRail
          documentId={EMPTY_DOCUMENT_ID}
          reviewProvider={reviewProvider}
          chatProvider={chatProvider}
          conversationId={EMPTY_CONVERSATION_ID}
        />
      }
    />
  );
}

/**
 * Live mode (the default on a ledger-backed scope). The bridge shares one Y.Doc, one adapter,
 * and one R2 pull across the editor and the rail, so cards and marks agree. A doc-scoped
 * SSE nudge reloads the review layer, and the aligned stream measures the editor's suggestion
 * marks through the anchor-rect source.
 */
export function CoworkLiveWorkspace({
  documentId,
  storeId,
  fallbackHealth,
}: {
  readonly documentId: string;
  readonly storeId: string;
  readonly fallbackHealth: CoworkHealthView | null;
}) {
  const conversationId = `cowork-doc-${documentId}`;

  // One document conversation linkage store per document. The submit path annotates a routing
  // note delivery here, and the feedback entry point annotates the captured span when R9 lands.
  const annotations = useMemo(() => new CoworkChatAnnotations(), [documentId]);

  // The rail store is owned here so the route-change guard reads the same staged sitting the
  // rail mutates, and the review keyboard binding comes from the settings registry.
  const [railStore] = useState(() => new RailStore({ tab: "review" }));
  const navBinding = useCoworkNavBinding();

  const bridge = useCoworkBridge({
    documentId,
    storeId,
    conversationId,
    onRoutingDelivery: (delivery) => annotations.annotateRoutingDelivery(delivery),
    // The last link of the feedback loop: R9 landed, so record the span-linked message on
    // the Chat tab and switch the rail to Chat so the human sees the feedback land.
    onFeedbackCaptured: (capture) => {
      annotations.annotateFeedback(capture);
      railStore.setTab("chat");
    },
  });

  // The union route-change guard (guards/routeGuard): a staged-but-unsubmitted sitting or an
  // unsent chat draft warns before a browser-level navigation. Read at event time, so it sees
  // the live sitting and the retained draft.
  const guardDirty = useCallback(
    () =>
      isDirty(railStore.getState()) ||
      isChatDraftDirty(loadChatDraft(window.localStorage, conversationId) ?? ""),
    [railStore, conversationId],
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
      railRef={bridge.railRef}
      editor={<CoworkBridgeEditor {...bridge.editorProps} />}
      rail={
        <CoworkRail
          documentId={documentId}
          reviewProvider={bridge.reviewProvider}
          chatProvider={bridge.chatProvider}
          conversationId={conversationId}
          anchorRects={bridge.anchorRects}
          store={railStore}
          queueBindings={navBinding}
          chatAnnotations={annotations}
          onScrollToChatAnchor={bridge.scrollToSpanAnchor}
        />
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
