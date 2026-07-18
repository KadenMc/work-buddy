/**
 * The wiring hook the Co-work surface uses in live mode. It assembles the whole live bridge
 * once per (documentId, storeId): a shared Y.Doc, the tracked-change adapter bound to it, the
 * Yjs transport, the R2 doc client, the live review provider, the ingestor, the anchor-rect
 * source, and the sitting transport. The rail and the editor then share ONE adapter and ONE
 * pull, so cards and marks stay in agreement.
 *
 * Data flow. The review provider's single R2 pull feeds the rail cards (its load return) and
 * the editor marks (its onProposals emission, consumed by the ingestor) and the health strip
 * (its onData emission). The editor mounts through CoworkBridgeEditor and reports its ready
 * context up (onReady), which attaches the ingestor to the adapter, records the DOM root for
 * the anchor measurements, and re-ingests whatever the pull already delivered.
 *
 * Every transport is injectable so the whole bridge is testable with in-memory doubles, and
 * defaults to the same-origin HTTP realizations for the live surface.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { Editor } from "@tiptap/core";
import * as Y from "yjs";

import { HttpCoworkYdocTransport } from "../persistence/HttpCoworkYdocTransport";
import type { CoworkYdocTransport } from "../persistence/transport";
import {
  HttpCoworkSittingTransport,
  type CoworkSittingTransport,
} from "../suggestions/sitting";
import { createWbTrackedChangesAdapter } from "../suggestions/adapter";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import type { ChatConversationProvider } from "../../../widget-library/chat";
import type { RoutingDeliveryInput, ScrollAnchorTarget } from "../chat";
import type { RailDriftHealth, ReviewRailData } from "../rail/contracts";
import type { AnchorRectSource } from "../rail/provider";
import { DomAnchorRectSource } from "./DomAnchorRectSource";
import {
  HttpCoworkDocClient,
  type CoworkDocClient,
} from "./HttpCoworkDocClient";
import { LiveReviewRailProvider } from "./LiveReviewRailProvider";
import { ProposalIngestor } from "./proposalIngestor";
import { createEditorMaterializeRenderer } from "./materialize";
import { resolveCoworkChatProvider } from "./chatProvider";
import type { CoworkEditorReadyContext } from "./CoworkBridgeEditor";

/** The default markdown a brand-new live document is seeded with, matching the demo pane. */
export const DEFAULT_BRIDGE_SEED_MARKDOWN = [
  "# Co-work document",
  "",
  "This is the editor pane. It binds a Tiptap editor to a local Y.Doc through the",
  "eight-point load-order contract, projects AI proposals as an ephemeral review layer,",
  "and materializes edits block by block.",
  "",
].join("\n");

/** The health projection the top health strip renders in live mode. */
export interface CoworkLiveHealth {
  readonly title: string;
  readonly drift: RailDriftHealth;
}

export interface UseCoworkBridgeOptions {
  readonly documentId: string;
  readonly storeId: string;
  readonly conversationId: string;
  /** True in demo / widget-lab / test mode, so the chat fixture is used deliberately. */
  readonly chatFixture?: boolean;
  readonly seedMarkdown?: string;
  /** Injectable R2 client, else the same-origin HTTP client. */
  readonly docClient?: CoworkDocClient;
  /** Injectable Yjs transport, else the same-origin HTTP transport. */
  readonly ydocTransport?: CoworkYdocTransport;
  /** Injectable sitting transport, else the same-origin HTTP transport. */
  readonly sittingTransport?: CoworkSittingTransport;
  /** Notified per routed item after a submit, so the Chat tab annotates the routing note. */
  readonly onRoutingDelivery?: (delivery: RoutingDeliveryInput) => void;
}

export interface CoworkBridgeEditorMountProps {
  readonly document: Y.Doc;
  readonly adapter: ReturnType<typeof createWbTrackedChangesAdapter>;
  readonly transport: CoworkYdocTransport;
  readonly seedMarkdown: string;
  readonly onReady: (context: CoworkEditorReadyContext) => void;
  readonly onTeardown: () => void;
}

export interface CoworkBridge {
  readonly reviewProvider: LiveReviewRailProvider;
  readonly chatProvider: ChatConversationProvider;
  readonly anchorRects: AnchorRectSource;
  readonly editorProps: CoworkBridgeEditorMountProps;
  /** Ref callback for the rail region, the anchor-rect coordinate root lives inside it. */
  readonly railRef: (element: HTMLElement | null) => void;
  /** Latest live health, or null before the first pull resolves. */
  readonly health: CoworkLiveHealth | null;
  /**
   * Bring a feedback span's passage into view. The Chat tab's scroll-to affordance is
   * span-keyed, so it carries the span's quote anchor, which resolves to an editor position
   * the same way a proposal does. A target with no anchor (span id only) degrades to a no-op,
   * because mapping a bare span id to a position needs the expression payload the doc-open
   * pull does not deliver in v1.
   */
  readonly scrollToSpanAnchor: (target: ScrollAnchorTarget) => void;
}

/** Find the aligned card-list inside the rail region, the anchor-rect coordinate root. */
const resolveRailRoot = (railRegion: HTMLElement | null): HTMLElement | null => {
  if (railRegion === null) return null;
  return (
    railRegion.querySelector<HTMLElement>(
      '.wb-cowork-rail__stream[data-aligned="true"] .wb-cowork-rail__card-list',
    ) ?? railRegion.querySelector<HTMLElement>(".wb-cowork-rail__card-list")
  );
};

export const useCoworkBridge = (
  options: UseCoworkBridgeOptions,
): CoworkBridge => {
  const {
    documentId,
    storeId,
    conversationId,
    chatFixture = false,
    seedMarkdown = DEFAULT_BRIDGE_SEED_MARKDOWN,
    docClient,
    ydocTransport,
    sittingTransport,
    onRoutingDelivery,
  } = options;

  const editorRef = useRef<Editor | null>(null);
  const editorDomRef = useRef<HTMLElement | null>(null);
  const railRegionRef = useRef<HTMLElement | null>(null);
  const editorReadyRef = useRef(false);
  const [health, setHealth] = useState<CoworkLiveHealth | null>(null);

  // Kept in a ref so the review provider stays stable per (documentId, storeId) while always
  // routing a delivery through the surface's latest callback.
  const onRoutingDeliveryRef = useRef(onRoutingDelivery);
  onRoutingDeliveryRef.current = onRoutingDelivery;

  const core = useMemo(() => {
    const doc = new Y.Doc();
    const adapter = createWbTrackedChangesAdapter({ doc });
    const ingestor = new ProposalIngestor();

    const resolvedDocClient =
      docClient ?? new HttpCoworkDocClient({ documentId, storeId });
    const resolvedYdocTransport =
      ydocTransport ?? new HttpCoworkYdocTransport({ documentId, storeId });
    const resolvedSittingTransport =
      sittingTransport ?? new HttpCoworkSittingTransport();

    const renderMaterialized = createEditorMaterializeRenderer(
      () => editorRef.current,
    );

    const reviewProvider = new LiveReviewRailProvider({
      docClient: resolvedDocClient,
      documentId,
      storeId,
      sittingTransport: resolvedSittingTransport,
      getAdapter: () => (editorReadyRef.current ? adapter : null),
      renderMaterialized,
      onRoutingDelivery: (delivery) => onRoutingDeliveryRef.current?.(delivery),
    });

    const anchorRects = new DomAnchorRectSource({
      getEditorRoot: () => editorDomRef.current,
      getRailRoot: () => resolveRailRoot(railRegionRef.current),
      adapter,
    });

    return {
      doc,
      adapter,
      ingestor,
      reviewProvider,
      anchorRects,
      ydocTransport: resolvedYdocTransport,
      renderMaterialized,
    };
    // The transports and clients are stable per (documentId, storeId). A test passes fresh
    // doubles for a fresh document, which is exactly when the whole bridge should rebuild.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documentId, storeId]);

  const chatProvider = useMemo(
    () => resolveCoworkChatProvider({ conversationId, fixture: chatFixture }),
    [conversationId, chatFixture],
  );

  // Drive ingestion and the health strip from the provider's single pull.
  useEffect(() => {
    const stopProposals = core.reviewProvider.onProposals((proposals) => {
      core.ingestor.setProposals(proposals);
    });
    const stopData = core.reviewProvider.onData((data: ReviewRailData) => {
      setHealth({ title: data.title, drift: data.drift });
    });
    return () => {
      stopProposals();
      stopData();
    };
  }, [core]);

  const editorProps = useMemo<CoworkBridgeEditorMountProps>(
    () => ({
      document: core.doc,
      adapter: core.adapter,
      transport: core.ydocTransport,
      seedMarkdown,
      onReady: ({ editor, dom }) => {
        editorRef.current = editor;
        editorDomRef.current = dom;
        editorReadyRef.current = true;
        core.ingestor.attach(core.adapter);
      },
      onTeardown: () => {
        editorReadyRef.current = false;
        editorRef.current = null;
        editorDomRef.current = null;
        core.ingestor.detach();
      },
    }),
    [core, seedMarkdown],
  );

  const railRef = useMemo(
    () => (element: HTMLElement | null) => {
      railRegionRef.current = element;
    },
    [],
  );

  const scrollToSpanAnchor = useMemo(
    () =>
      (target: ScrollAnchorTarget): void => {
        const editor = editorRef.current;
        const anchor = target.anchor;
        if (editor === null || anchor === undefined) return;
        const range = resolveQuoteAnchor(editor.state.doc, {
          exact: anchor.exact,
          prefix: anchor.prefix ?? "",
          suffix: anchor.suffix ?? "",
        });
        if (range === null) return;
        editor.chain().setTextSelection(range).scrollIntoView().run();
      },
    [],
  );

  return {
    reviewProvider: core.reviewProvider,
    chatProvider,
    anchorRects: core.anchorRects,
    editorProps,
    railRef,
    health,
    scrollToSpanAnchor,
  };
};
