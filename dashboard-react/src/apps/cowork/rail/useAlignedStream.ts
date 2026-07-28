/**
 * The imperative controller for the aligned margin-card stream. It measures card
 * heights and anchor tops and writes card positions straight to the DOM, OUTSIDE
 * the React render cycle, to hold the sub-16 ms paint budget (perf contract,
 * audit A12). React never re-renders on a scroll or a resize, the controller
 * just moves the cards.
 *
 * Alignment activates only when an AnchorRectSource is wired, since the editor
 * is the sole owner of live anchor geometry. With no source the stream degrades
 * to a document-order list and the cards keep their normal flow position, which
 * is the shipped default (the scroll-to-and-highlight path).
 */

import { useCallback, useEffect, useMemo, useRef } from "react";

import type { AnchorRectSource } from "./provider";
import type { RailSelectionKind } from "./store";
import {
  computeAlignedLayout,
  placementsEqual,
  type AlignInput,
  type AlignPlacement,
} from "./geometry";

export interface UseAlignedStreamOptions {
  /** The editor anchor-rect seam. Absent means the degrade path (normal flow). */
  readonly anchorRects?: AnchorRectSource;
  /** Namespace-qualified review anchors in document order. */
  readonly anchors: readonly AlignedAnchorIdentity[];
  /** Minimum vertical gap between stacked cards. */
  readonly gap?: number;
}

export interface AlignedStreamController {
  /** True when per-anchor alignment is active. */
  readonly aligned: boolean;
  /** Ref callback to register a card element by namespace-qualified review identity. */
  registerCard(
    id: string,
    kind: RailSelectionKind,
  ): (element: HTMLElement | null) => void;
  /** Ref callback for the scroll container the cards are positioned within. */
  registerContainer(element: HTMLElement | null): void;
}

export interface AlignedAnchorIdentity {
  readonly id: string;
  readonly kind: RailSelectionKind;
}

const alignmentKey = (anchor: AlignedAnchorIdentity): string =>
  `${anchor.kind}:${anchor.id}`;

function schedule(callback: () => void): number {
  if (typeof requestAnimationFrame === "function") {
    return requestAnimationFrame(callback);
  }
  callback();
  return 0;
}

function cancel(handle: number): void {
  if (handle !== 0 && typeof cancelAnimationFrame === "function") {
    cancelAnimationFrame(handle);
  }
}

export function useAlignedStream(
  options: UseAlignedStreamOptions,
): AlignedStreamController {
  const { anchorRects, anchors, gap } = options;
  const aligned = anchorRects !== undefined;

  const cardsRef = useRef(new Map<string, HTMLElement>());
  const containerRef = useRef<HTMLElement | null>(null);
  const lastPlacementRef = useRef<AlignPlacement[]>([]);
  const frameRef = useRef(0);
  // The id order is read imperatively during measurement, so keep it current
  // without re-subscribing the geometry listeners on every render.
  const anchorsRef = useRef<readonly AlignedAnchorIdentity[]>(anchors);
  anchorsRef.current = anchors;

  const clearPlacementStyles = useCallback(() => {
    for (const element of cardsRef.current.values()) {
      element.style.removeProperty("position");
      element.style.removeProperty("inset-inline-start");
      element.style.removeProperty("inset-inline-end");
      element.style.removeProperty("top");
      element.style.removeProperty("transform");
    }
    const container = containerRef.current;
    if (container !== null) {
      container.style.removeProperty("position");
      container.style.removeProperty("min-block-size");
    }
  }, []);

  const measure = useCallback(() => {
    const source = anchorRects;
    const container = containerRef.current;
    if (source === undefined || container === null) return;

    const inputs: AlignInput[] = [];
    let unresolved = false;
    for (const anchor of anchorsRef.current) {
      const key = alignmentKey(anchor);
      const element = cardsRef.current.get(key);
      if (element === undefined) continue;
      const rect = source.anchorRect(anchor.id, anchor.kind);
      if (rect === null) {
        unresolved = true;
        continue;
      }
      inputs.push({ id: key, anchorTop: rect.top, height: element.offsetHeight });
    }

    // Mixing absolutely positioned cards with normal-flow cards lets the two
    // groups overlap, and retaining an old transform lies after an anchor is
    // lost. If any rendered card cannot resolve, degrade the whole stream.
    if (unresolved) {
      clearPlacementStyles();
      lastPlacementRef.current = [];
      return;
    }

    const placements = computeAlignedLayout(inputs, { gap });
    if (placementsEqual(placements, lastPlacementRef.current)) return;
    clearPlacementStyles();
    lastPlacementRef.current = placements;

    container.style.position = "relative";
    let maxBottom = 0;
    for (const placement of placements) {
      const element = cardsRef.current.get(placement.id);
      if (element === undefined) continue;
      element.style.position = "absolute";
      element.style.insetInlineStart = "0";
      element.style.insetInlineEnd = "0";
      element.style.top = "0";
      element.style.transform = `translateY(${placement.top}px)`;
      maxBottom = Math.max(maxBottom, placement.top + element.offsetHeight);
    }
    container.style.minBlockSize = `${maxBottom}px`;
  }, [anchorRects, clearPlacementStyles, gap]);

  const requestMeasure = useCallback(() => {
    cancel(frameRef.current);
    frameRef.current = schedule(measure);
  }, [measure]);

  useEffect(() => {
    if (!aligned || anchorRects === undefined) return undefined;
    requestMeasure();
    const unsubscribe = anchorRects.subscribe(requestMeasure);

    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver === "function") {
      observer = new ResizeObserver(requestMeasure);
      for (const element of cardsRef.current.values()) observer.observe(element);
    }

    return () => {
      cancel(frameRef.current);
      unsubscribe();
      observer?.disconnect();
      clearPlacementStyles();
      lastPlacementRef.current = [];
    };
  }, [
    aligned,
    anchorRects,
    requestMeasure,
    anchors,
    clearPlacementStyles,
  ]);

  const registerCard = useCallback(
    (id: string, kind: RailSelectionKind) => (element: HTMLElement | null) => {
      const key = alignmentKey({ id, kind });
      if (element === null) {
        const previous = cardsRef.current.get(key);
        previous?.style.removeProperty("position");
        previous?.style.removeProperty("inset-inline-start");
        previous?.style.removeProperty("inset-inline-end");
        previous?.style.removeProperty("top");
        previous?.style.removeProperty("transform");
        cardsRef.current.delete(key);
        return;
      }
      cardsRef.current.set(key, element);
      if (aligned) requestMeasure();
    },
    [aligned, requestMeasure],
  );

  const registerContainer = useCallback(
    (element: HTMLElement | null) => {
      if (element === null && containerRef.current !== null) {
        containerRef.current.style.removeProperty("position");
        containerRef.current.style.removeProperty("min-block-size");
      }
      containerRef.current = element;
      if (aligned) requestMeasure();
    },
    [aligned, requestMeasure],
  );

  return useMemo(
    () => ({ aligned, registerCard, registerContainer }),
    [aligned, registerCard, registerContainer],
  );
}
