import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

interface HoverState {
  readonly left: number;
  readonly top: number;
  readonly anchorLeft: number;
  readonly anchorTop: number;
  readonly anchorBottom: number;
  readonly authorship: string;
  readonly review: string;
  readonly source: string;
  readonly sourceDetail: string;
  readonly contributors: string;
  readonly reviewers: string;
  readonly attester: string;
  readonly basis: string;
  readonly historyCount: string;
  readonly conflicted: boolean;
  readonly recordState: string;
  readonly currentness: string;
}

const label = (value: string): string => value.replace(/_/gu, " ");

const expandedSelectionTouches = (root: HTMLElement): boolean => {
  const selection = document.getSelection();
  if (selection === null || selection.isCollapsed) return false;
  return (
    (selection.anchorNode !== null && root.contains(selection.anchorNode)) ||
    (selection.focusNode !== null && root.contains(selection.focusNode))
  );
};

/** Passive explanation for provenance-decorated text; all actions stay in the panel. */
export function ProvenanceHoverCard({
  rootRef,
  active,
  editorReady,
}: {
  readonly rootRef: RefObject<HTMLElement | null>;
  readonly active: boolean;
  /** Reactive mount signal for the otherwise non-reactive editor root ref. */
  readonly editorReady: boolean;
}) {
  const [hover, setHover] = useState<HoverState | null>(null);
  const cardRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const root = rootRef.current;
    if (!active || root === null) {
      setHover(null);
      return undefined;
    }
    let hoveredDecoration: HTMLElement | null = null;
    let pointerPosition: { readonly x: number; readonly y: number } | null =
      null;
    const showElement = (target: Element | null): void => {
      // A deliberate text selection owns the contextual UI. Mouse-dragging a
      // decorated passage first fires pointerover, so without this guard the
      // passive card remains above and completely covers the selection action.
      if (expandedSelectionTouches(root)) {
        hoveredDecoration = null;
        setHover(null);
        return;
      }
      const element =
        target?.closest<HTMLElement>(
          "[data-wb-decoration='provenance-overlay']",
        ) ?? null;
      if (element === null || !root.contains(element)) {
        hoveredDecoration = null;
        setHover(null);
        return;
      }
      hoveredDecoration = element;
      const rect = element.getBoundingClientRect();
      setHover({
        left: Math.max(8, rect.left),
        top: Math.max(8, rect.bottom + 8),
        anchorLeft: rect.left,
        anchorTop: rect.top,
        anchorBottom: rect.bottom,
        authorship: element.dataset.wbAuthorship ?? "unknown",
        review: element.dataset.wbHumanReview ?? "unknown",
        source: element.dataset.wbSource ?? "unknown",
        sourceDetail:
          element.dataset.wbSourceDetail ?? "No additional source detail",
        contributors:
          element.dataset.wbContributors ?? "No contributors recorded",
        reviewers: element.dataset.wbReviewers ?? "No reviewers recorded",
        attester: element.dataset.wbAttester ?? "not recorded",
        basis: element.dataset.wbBasis ?? "not recorded",
        historyCount: element.dataset.wbHistoryCount ?? "0",
        conflicted: element.dataset.wbProvenanceConflict === "true",
        recordState: element.dataset.wbProvenanceRecordState ?? "recorded",
        currentness: element.dataset.wbProvenanceCurrentness ?? "unknown",
      });
    };
    const show = (event: Event): void => {
      if ("clientX" in event && "clientY" in event) {
        const x = Number(event.clientX);
        const y = Number(event.clientY);
        if (Number.isFinite(x) && Number.isFinite(y)) {
          pointerPosition = { x, y };
        }
      }
      showElement(event.target instanceof Element ? event.target : null);
    };
    const trackPointer = (event: PointerEvent): void => {
      pointerPosition = { x: event.clientX, y: event.clientY };
    };
    const showSelection = (): void => {
      if (expandedSelectionTouches(root)) {
        setHover(null);
        return;
      }
      if (!root.contains(document.activeElement)) return;
      const anchorNode = document.getSelection()?.anchorNode ?? null;
      const element =
        anchorNode instanceof Element
          ? anchorNode
          : (anchorNode?.parentElement ?? null);
      showElement(element);
    };
    const hide = (event: Event): void => {
      if (
        event instanceof FocusEvent &&
        event.relatedTarget instanceof Node &&
        root.contains(event.relatedTarget)
      )
        return;
      hoveredDecoration = null;
      pointerPosition = null;
      setHover(null);
    };
    const refreshHoveredDecoration = (): void => {
      if (hoveredDecoration === null) return;
      if (hoveredDecoration.isConnected && root.contains(hoveredDecoration)) {
        showElement(hoveredDecoration);
        return;
      }
      const pointed =
        pointerPosition === null ||
        typeof document.elementFromPoint !== "function"
          ? null
          : document.elementFromPoint(pointerPosition.x, pointerPosition.y);
      showElement(pointed);
    };
    const observer = new MutationObserver(refreshHoveredDecoration);
    observer.observe(root, {
      attributes: true,
      childList: true,
      subtree: true,
      attributeFilter: [
        "data-wb-decoration",
        "data-wb-authorship",
        "data-wb-human-review",
        "data-wb-source",
        "data-wb-source-detail",
        "data-wb-contributors",
        "data-wb-reviewers",
        "data-wb-attester",
        "data-wb-basis",
        "data-wb-history-count",
        "data-wb-provenance-conflict",
        "data-wb-provenance-record-state",
        "data-wb-provenance-currentness",
      ],
    });
    root.addEventListener("pointerover", show);
    root.addEventListener("pointermove", trackPointer);
    root.addEventListener("focusin", show);
    root.addEventListener("pointerleave", hide);
    root.addEventListener("focusout", hide);
    const escape = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setHover(null);
    };
    document.addEventListener("keydown", escape);
    document.addEventListener("selectionchange", showSelection);
    return () => {
      observer.disconnect();
      root.removeEventListener("pointerover", show);
      root.removeEventListener("pointermove", trackPointer);
      root.removeEventListener("focusin", show);
      root.removeEventListener("pointerleave", hide);
      root.removeEventListener("focusout", hide);
      document.removeEventListener("keydown", escape);
      document.removeEventListener("selectionchange", showSelection);
    };
  }, [active, editorReady, rootRef]);
  useLayoutEffect(() => {
    const card = cardRef.current;
    if (hover === null || card === null) return;
    const gutter = 8;
    const rect = card.getBoundingClientRect();
    const width = Math.max(rect.width, card.offsetWidth);
    const height = Math.max(rect.height, card.offsetHeight);
    const left = Math.max(
      gutter,
      Math.min(hover.anchorLeft, window.innerWidth - width - gutter),
    );
    const below = hover.anchorBottom + gutter;
    const top = Math.max(
      gutter,
      below + height <= window.innerHeight - gutter
        ? below
        : Math.min(
            hover.anchorTop - height - gutter,
            window.innerHeight - height - gutter,
          ),
    );
    if (left !== hover.left || top !== hover.top) {
      setHover((current) =>
        current === null ? null : { ...current, left, top },
      );
    }
  }, [hover]);
  if (!active || hover === null) return null;
  const pending = hover.recordState === "pending";
  return createPortal(
    <aside
      className="wb-cowork-provenance-hover"
      role="tooltip"
      ref={cardRef}
      style={{ left: hover.left, top: hover.top }}
    >
      <strong>
        {hover.conflicted
          ? "Conflicting provenance"
          : pending
            ? "Recording provenance…"
            : hover.recordState === "unrecorded"
              ? "No provenance recorded"
              : `${label(hover.authorship)} authorship`}
      </strong>
      {pending ? (
        <p>
          This recent typing is captured in the editor while its provenance
          record is saved. Authorship and review appear after the server
          confirms the record.
        </p>
      ) : (
        <>
          <dl>
            <div>
              <dt>Human review</dt>
              <dd>{label(hover.review)}</dd>
            </div>
            <div>
              <dt>Contributors</dt>
              <dd>{hover.contributors}</dd>
            </div>
            <div>
              <dt>Reviewers</dt>
              <dd>{hover.reviewers}</dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd>
                {label(hover.source)} · {hover.sourceDetail}
              </dd>
            </div>
            <div>
              <dt>Attested by</dt>
              <dd>{hover.attester}</dd>
            </div>
            <div>
              <dt>Basis</dt>
              <dd>{label(hover.basis)}</dd>
            </div>
            <div>
              <dt>Target</dt>
              <dd>{label(hover.currentness)}</dd>
            </div>
            <div>
              <dt>History</dt>
              <dd>{hover.historyCount} records</dd>
            </div>
          </dl>
          <p>Open Provenance for details and actions.</p>
        </>
      )}
    </aside>,
    document.body,
  );
}
