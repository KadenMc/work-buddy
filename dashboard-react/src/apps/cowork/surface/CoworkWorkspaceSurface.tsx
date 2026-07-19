import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import type { SingleSurfaceRuntimeProps } from "../../../dashboard/contributions/viewModules";
import { useOptionalDashboardEvents } from "../../../dashboard/events/DashboardEventProvider";
import { useViewSession } from "../../../dashboard/views/useViewSession";
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
} from "../rail";
import "./styles.css";

const DRIFT_LABEL: Record<string, string> = {
  clean: "In sync",
  drifted: "Drifted from file",
  missing: "File missing",
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
  );
}

/** The shared three-region shell, so demo and live compose the same layout (section 5). */
function CoworkWorkspaceLayout({
  label,
  health,
  editor,
  rail,
  railRef,
}: {
  readonly label: string;
  readonly health: CoworkHealthView | null;
  readonly editor: ReactNode;
  readonly rail: ReactNode;
  readonly railRef?: (element: HTMLElement | null) => void;
}) {
  return (
    <main className="wb-cowork" aria-label={label}>
      <CoworkHealthStrip health={health} />
      <div className="wb-cowork__body">
        <div className="wb-cowork__editor-region">{editor}</div>
        <aside className="wb-cowork__rail" aria-label="Review and chat" ref={railRef}>
          {rail}
        </aside>
      </div>
    </main>
  );
}

const healthFromModel = (model: CoworkViewModel | null): CoworkHealthView | null => {
  const document = model?.document ?? null;
  if (document === null) return null;
  return {
    title: document.title,
    driftState: document.driftState,
    openProposalCount: document.openProposalCount,
  };
};

/**
 * Demo mode (the fixture switch). The in-memory review and demo chat providers back the
 * rail and the demo editor pane runs an in-memory Yjs transport, so widget-lab, the tests,
 * and an offline shell all render the same deterministic scene with no network.
 */
function CoworkDemoWorkspace({
  label,
  model,
}: {
  readonly label: string;
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
      label={label}
      health={healthFromModel(model)}
      editor={<CoworkEditorPane />}
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
 * Live mode (the default on a ledger-backed scope). The bridge shares one Y.Doc, one adapter,
 * and one R2 pull across the editor and the rail, so cards and marks agree. A doc-scoped
 * SSE nudge reloads the review layer, and the aligned stream measures the editor's suggestion
 * marks through the anchor-rect source.
 */
function CoworkLiveWorkspace({
  label,
  documentId,
  storeId,
  fallbackHealth,
}: {
  readonly label: string;
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
      label={label}
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

type CoworkFixtureMode = "demo" | "live";

/**
 * Decide demo vs live. An explicit `?cowork_fixture=` query wins (widget-lab and manual
 * testing), else a demo-quality coarse snapshot is demo, else a live scope with a resolvable
 * store id and document id is live. Live needs the store id, supplied on navigation as the
 * same `store_id` the routes take, so a live scope with no store id degrades safely to demo.
 */
function resolveFixtureMode(
  quality: string | undefined,
  documentId: string | undefined,
  storeId: string | undefined,
  override: string | null,
): CoworkFixtureMode {
  if (override === "demo") return "demo";
  const wantLive = override === "live" || quality !== "demo";
  if (wantLive && documentId !== undefined && storeId !== undefined) return "live";
  return "demo";
}

/**
 * The App-owned Co-work surface renderer (section 5, variant-A-hybrid). It composes the
 * three regions inside ONE React tree that shares the coarse document session: the header
 * health strip on top, the editor pane center-left, and the Review / Chat tabbed rail on
 * the right. The coarse session flows through the ViewProvider snapshot, and the live Y.Doc
 * and the sitting take the direct route to `/api/truth/doc/*`. Demo mode keeps the
 * deterministic in-memory scene for widget-lab and tests behind the fixture switch.
 */
export function CoworkWorkspaceSurface({
  definition,
  provider,
}: SingleSurfaceRuntimeProps) {
  const session = useViewSession({ provider, viewId: definition.viewId });
  const model = (session.snapshot?.model as CoworkViewModel | undefined) ?? null;
  const documentId = model?.document?.documentId;

  const search = typeof window === "undefined" ? "" : window.location.search;
  const { storeId, override } = useMemo(() => {
    const params = new URLSearchParams(search);
    return {
      storeId: params.get("store_id") ?? undefined,
      override: params.get("cowork_fixture"),
    };
  }, [search]);

  const mode = resolveFixtureMode(
    session.snapshot?.quality.kind,
    documentId,
    storeId,
    override,
  );

  if (mode === "live" && documentId !== undefined && storeId !== undefined) {
    return (
      <CoworkLiveWorkspace
        label={definition.displayName}
        documentId={documentId}
        storeId={storeId}
        fallbackHealth={healthFromModel(model)}
      />
    );
  }

  return <CoworkDemoWorkspace label={definition.displayName} model={model} />;
}

export default CoworkWorkspaceSurface;
