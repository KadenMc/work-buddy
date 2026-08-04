/**
 * The wiring hook the Co-work surface uses in live mode. It assembles the whole live bridge
 * once per (documentId, storeId): a canonical Y.Doc, the Yjs transport, the R2 doc client,
 * the live review provider, the view-only decoration projector, the Review anchor controller, and
 * the sitting transport. The rail and editor consume ONE pull, so cards and decorations stay
 * in agreement without proposal structs entering the collaborative document.
 *
 * Data flow. The review provider's single R2 pull feeds the rail cards (its load return) and
 * the editor decorations and health strip (its onData emission), while its ProposalInput
 * channel is retained as the authoritative sitting catalog. The editor mounts through
 * CoworkBridgeEditor and reports its ready context up so the projector and anchor controller
 * can attach.
 *
 * Every transport is injectable so the whole bridge is testable with in-memory doubles, and
 * defaults to the same-origin HTTP realizations for the live surface.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { Editor } from "@tiptap/core";
import * as Y from "yjs";

import { HttpCoworkYdocTransport } from "../persistence/HttpCoworkYdocTransport";
import type { CoworkYdocTransport } from "../persistence/transport";
import type { CoworkSyncStatus } from "../persistence/CoworkYdocPersistence";
import type { CoworkDriftState } from "../contracts";
import type {
  CoworkMaterializationController,
  CoworkMaterializationState,
  CoworkMaterializeReceipt,
} from "../materialization/contracts";
import {
  HttpCoworkSittingTransport,
  type CoworkSittingTransport,
} from "../suggestions/sitting";
import type { ProposalInput } from "../suggestions/types";
import type {
  FeedbackCapture,
  RoutingDeliveryInput,
  ScrollAnchorTarget,
} from "../chat";
import type { CoworkFeedbackTransport } from "../feedback";
import type {
  CoworkVerifyCapability,
  RailDriftHealth,
  ReviewRailData,
  VerificationRecheckIntent,
} from "../rail/contracts";
import type { ReviewAnchorController } from "../rail/provider";
import { DomReviewAnchorController } from "./DomReviewAnchorController";
import {
  HttpCoworkDocClient,
  type CoworkDocClient,
} from "./HttpCoworkDocClient";
import { CoworkPassageHighlighter } from "./CoworkPassageHighlighter";
import {
  LiveReviewRailProvider,
  type VerifyRecheckRequest,
} from "./LiveReviewRailProvider";
import { LedgerDecorationProjector } from "./ledgerDecorationProjector";
import type { CoworkEditorReadyContext } from "./CoworkBridgeEditor";
import type { CoworkSittingWorkspace } from "./sittingWorkspace";
import type { CoworkActionSnapshotController } from "../targets";
import type { CoworkPasteProvenanceRecorder } from "../provenance";

/** Registered documents are initialized by bootstrap; live hydration never fabricates text. */
export const DEFAULT_BRIDGE_SEED_MARKDOWN = "";

/** The health projection the top health strip renders in live mode. */
export interface CoworkLiveHealth {
  readonly title: string;
  readonly drift: RailDriftHealth;
}

export interface UseCoworkBridgeOptions {
  readonly documentId: string;
  readonly storeId: string;
  readonly seedMarkdown?: string;
  readonly readOnly?: boolean;
  readonly onSyncStatus?: (status: CoworkSyncStatus) => void;
  readonly currentFileSha256?: string | null;
  readonly initialDriftState?: CoworkDriftState;
  readonly canMaterialize?: boolean;
  readonly onMaterializationState?: (state: CoworkMaterializationState) => void;
  readonly onMaterializationController?: (
    controller: CoworkMaterializationController | null,
  ) => void;
  readonly onMaterialized?: (receipt: CoworkMaterializeReceipt) => void;
  /** Injectable R2 client, else the same-origin HTTP client. */
  readonly docClient?: CoworkDocClient;
  /** Injectable Yjs transport, else the same-origin HTTP transport. */
  readonly ydocTransport?: CoworkYdocTransport;
  /** Injectable sitting transport, else the same-origin HTTP transport. */
  readonly sittingTransport?: CoworkSittingTransport;
  /** Notified per routed item after a submit, so the Chat tab annotates the routing note. */
  readonly onRoutingDelivery?: (delivery: RoutingDeliveryInput) => void;
  /** Applied Verify corrections after the authoritative structured version is pulled. */
  readonly onSittingCommitted?: (
    requests: readonly VerifyRecheckRequest[],
  ) => void;
  /**
   * Notified after a successful R9 feedback capture, so the surface annotates the
   * span-linked message on the Chat tab and switches the rail to Chat.
   */
  readonly onFeedbackCaptured?: (capture: FeedbackCapture) => void;
  /** Injectable R9 feedback transport, else the same-origin HTTP transport. */
  readonly feedbackTransport?: CoworkFeedbackTransport;
  /** Persists exact-span provenance for editor paste transactions. */
  readonly pasteProvenanceRecorder?: CoworkPasteProvenanceRecorder;
}

export interface CoworkBridgeEditorMountProps {
  readonly document: Y.Doc;
  readonly transport: CoworkYdocTransport;
  readonly seedMarkdown: string;
  readonly onReady: (context: CoworkEditorReadyContext) => void;
  readonly onTeardown: () => void;
  /** The cowork doc id, for the R9 feedback affordance mounted in the editor host. */
  readonly documentId: string;
  /** The scope store id the R9 feedback route takes. */
  readonly storeId: string;
  /** Notified with the R9 capture, wired by the surface to the Chat tab. */
  readonly onFeedbackCaptured?: (capture: FeedbackCapture) => void;
  /** Injectable R9 feedback transport, else the same-origin HTTP transport. */
  readonly feedbackTransport?: CoworkFeedbackTransport;
  /** Persists exact-span provenance for editor paste transactions. */
  readonly onRecordPasteProvenance?: CoworkPasteProvenanceRecorder;
  readonly readOnly?: boolean;
  readonly onSyncStatus?: (status: CoworkSyncStatus) => void;
  readonly currentFileSha256?: string | null;
  readonly initialDriftState?: CoworkDriftState;
  readonly canMaterialize?: boolean;
  readonly onMaterializationState?: (state: CoworkMaterializationState) => void;
  readonly onMaterializationController?: (
    controller: CoworkMaterializationController | null,
  ) => void;
  readonly onMaterialized?: (receipt: CoworkMaterializeReceipt) => void;
  readonly onSittingWorkspace?: (workspace: CoworkSittingWorkspace | null) => void;
  readonly onActionSnapshotController?: (
    controller: CoworkActionSnapshotController | null,
  ) => void;
  readonly getProposalCatalog: () => readonly ProposalInput[];
  readonly onSittingServerRefreshed?: () => void;
}

export interface CoworkBridge {
  readonly reviewProvider: LiveReviewRailProvider;
  readonly reviewAnchors: ReviewAnchorController;
  readonly editorProps: CoworkBridgeEditorMountProps;
  /** Latest live health, or null before the first pull resolves. */
  readonly health: CoworkLiveHealth | null;
  /** Concise criterion counts for the editor-bottom Verify dock. */
  readonly verifySetup: {
    readonly activeCount: number;
    readonly unavailableCount: number;
  } | null;
  /** Fail-closed server negotiation for Verify and Co-think actions. */
  readonly verifyCapability: CoworkVerifyCapability | null;
  /** Exact target/action capture behavior owned by the currently mounted editor. */
  readonly actionSnapshotController: CoworkActionSnapshotController | null;
  /** Durable, restart-surviving rechecks from the latest authoritative R2 pull. */
  readonly verificationRecheckIntents: readonly VerificationRecheckIntent[];
  /**
   * Bring a feedback span's passage into view. The Chat tab's scroll-to affordance is
   * span-keyed, so it carries the span's quote anchor, which resolves to an editor position
   * the same way a proposal does. A target with no anchor (span id only) degrades to a no-op,
   * because mapping a bare span id to a position needs the expression payload the doc-open
   * pull does not deliver in v1.
   */
  readonly scrollToSpanAnchor: (target: ScrollAnchorTarget) => boolean;
}

export const useCoworkBridge = (
  options: UseCoworkBridgeOptions,
): CoworkBridge => {
  const {
    documentId,
    storeId,
    seedMarkdown = DEFAULT_BRIDGE_SEED_MARKDOWN,
    readOnly = false,
    onSyncStatus,
    currentFileSha256,
    initialDriftState,
    canMaterialize,
    onMaterializationState,
    onMaterializationController,
    onMaterialized,
    docClient,
    ydocTransport,
    sittingTransport,
    onRoutingDelivery,
    onSittingCommitted,
    onFeedbackCaptured,
    feedbackTransport,
    pasteProvenanceRecorder,
  } = options;

  const editorRef = useRef<Editor | null>(null);
  const editorDomRef = useRef<HTMLElement | null>(null);
  const editorReadyRef = useRef(false);
  const sittingWorkspaceRef = useRef<CoworkSittingWorkspace | null>(null);
  const proposalCatalogRef = useRef<readonly ProposalInput[]>([]);
  const [health, setHealth] = useState<CoworkLiveHealth | null>(null);
  const [verifySetup, setVerifySetup] = useState<{
    readonly activeCount: number;
    readonly unavailableCount: number;
  } | null>(null);
  const [verifyCapability, setVerifyCapability] =
    useState<CoworkVerifyCapability | null>(null);
  const [verificationRecheckIntents, setVerificationRecheckIntents] =
    useState<readonly VerificationRecheckIntent[]>([]);
  const [actionSnapshotController, setActionSnapshotController] =
    useState<CoworkActionSnapshotController | null>(null);

  // Kept in a ref so the review provider stays stable per (documentId, storeId) while always
  // routing a delivery through the surface's latest callback.
  const onRoutingDeliveryRef = useRef(onRoutingDelivery);
  onRoutingDeliveryRef.current = onRoutingDelivery;
  const onSittingCommittedRef = useRef(onSittingCommitted);
  onSittingCommittedRef.current = onSittingCommitted;

  // Same treatment for the feedback callback: a stable editorProps that always
  // routes a capture through the surface's latest callback.
  const onFeedbackCapturedRef = useRef(onFeedbackCaptured);
  onFeedbackCapturedRef.current = onFeedbackCaptured;
  const feedbackCaptureEnabled = onFeedbackCaptured !== undefined;

  const core = useMemo(() => {
    const doc = new Y.Doc();
    const ledgerProjector = new LedgerDecorationProjector();
    const passageHighlighter = new CoworkPassageHighlighter({
      getEditor: () => editorRef.current,
    });

    const resolvedDocClient =
      docClient ?? new HttpCoworkDocClient({ documentId, storeId });
    const resolvedYdocTransport =
      ydocTransport ?? new HttpCoworkYdocTransport({ documentId, storeId });
    const resolvedSittingTransport =
      sittingTransport ?? new HttpCoworkSittingTransport();

    const reviewProvider = new LiveReviewRailProvider({
      docClient: resolvedDocClient,
      documentId,
      storeId,
      sittingTransport: resolvedSittingTransport,
      getSittingWorkspace: () => sittingWorkspaceRef.current,
      onRoutingDelivery: (delivery) => onRoutingDeliveryRef.current?.(delivery),
      onSittingCommitted: (requests) =>
        onSittingCommittedRef.current?.(requests),
    });

    const reviewAnchors = new DomReviewAnchorController({
      getEditorRoot: () => editorDomRef.current,
      getEditor: () => editorRef.current,
    });

    return {
      doc,
      ledgerProjector,
      passageHighlighter,
      reviewProvider,
      reviewAnchors,
      ydocTransport: resolvedYdocTransport,
    };
    // The transports and clients are stable per (documentId, storeId). A test passes fresh
    // doubles for a fresh document, which is exactly when the whole bridge should rebuild.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId, storeId]);

  // Drive the view-only editor projection, sitting catalog, and health strip
  // from the provider's single authoritative pull.
  useEffect(() => {
    proposalCatalogRef.current = [];
    setVerificationRecheckIntents([]);
    const stopProposals = core.reviewProvider.onProposals((proposals) => {
      proposalCatalogRef.current = proposals;
    });
    const stopData = core.reviewProvider.onData((data: ReviewRailData) => {
      setHealth({ title: data.title, drift: data.drift });
      setVerifyCapability(data.verifyCapability);
      setVerificationRecheckIntents(data.verificationRecheckIntents);
      setVerifySetup({
        activeCount: data.verificationConfiguration.criteria.filter(
          (criterion) => criterion.operationalState === "active",
        ).length,
        unavailableCount: data.verificationConfiguration.criteria.filter(
          (criterion) =>
            criterion.operationalState === "unavailable" ||
            criterion.operationalState === "blocked_required_check",
        ).length,
      });
      core.ledgerProjector.setData(data);
      core.reviewAnchors.refresh();
    });
    return () => {
      proposalCatalogRef.current = [];
      stopProposals();
      stopData();
    };
  }, [core]);

  useEffect(
    () => () => {
      core.ledgerProjector.detach();
      core.passageHighlighter.dispose();
      // The editor and rail unsubscribe during the same unmount. Defer final destruction so
      // their cleanups can detach observers from an intact document first.
      queueMicrotask(() => core.doc.destroy());
    },
    [core],
  );

  const editorProps = useMemo<CoworkBridgeEditorMountProps>(
    () => ({
      document: core.doc,
      transport: core.ydocTransport,
      seedMarkdown,
      onReady: ({ editor, dom }) => {
        editorRef.current = editor;
        editorDomRef.current = dom;
        editorReadyRef.current = true;
        core.ledgerProjector.attach(editor);
        core.reviewAnchors.attachEditor(editor);
      },
      onTeardown: () => {
        core.passageHighlighter.clear();
        core.reviewAnchors.detachEditor();
        editorReadyRef.current = false;
        editorRef.current = null;
        editorDomRef.current = null;
        core.ledgerProjector.detach();
      },
      documentId,
      storeId,
      feedbackTransport,
      onRecordPasteProvenance: pasteProvenanceRecorder,
      ...(feedbackCaptureEnabled
        ? {
            onFeedbackCaptured: (capture: FeedbackCapture) =>
              onFeedbackCapturedRef.current?.(capture),
          }
        : {}),
      readOnly,
      onSyncStatus,
      currentFileSha256,
      initialDriftState,
      canMaterialize,
      onMaterializationState,
      onMaterializationController,
      onMaterialized,
      onSittingWorkspace: (workspace) => {
        sittingWorkspaceRef.current = workspace;
      },
      onActionSnapshotController: setActionSnapshotController,
      getProposalCatalog: () => proposalCatalogRef.current,
      onSittingServerRefreshed: () => {
        core.ledgerProjector.clear();
      },
    }),
    [
      core,
      seedMarkdown,
      documentId,
      storeId,
      feedbackTransport,
      pasteProvenanceRecorder,
      feedbackCaptureEnabled,
      readOnly,
      onSyncStatus,
      currentFileSha256,
      initialDriftState,
      canMaterialize,
      onMaterializationState,
      onMaterializationController,
      onMaterialized,
    ],
  );

  const scrollToSpanAnchor = useMemo(
    () =>
      (target: ScrollAnchorTarget): boolean =>
        core.passageHighlighter.show(target),
    [core],
  );

  return {
    reviewProvider: core.reviewProvider,
    reviewAnchors: core.reviewAnchors,
    editorProps,
    health,
    verifySetup,
    verifyCapability,
    verificationRecheckIntents,
    actionSnapshotController,
    scrollToSpanAnchor,
  };
};
