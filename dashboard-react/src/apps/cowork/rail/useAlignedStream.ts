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
import {
  computeAlignedLayout,
  placementsEqual,
  type AlignInput,
  type AlignPlacement,
} from "./geometry";

export interface UseAlignedStreamOptions {
  /** The editor anchor-rect seam. Absent means the degrade path (normal flow). */
  readonly anchorRects?: AnchorRectSource;
  /** Proposal ids in document order. */
  readonly ids: readonly string[];
  /** Minimum vertical gap between stacked cards. */
  readonly gap?: number;
}

export interface AlignedStreamController {
  /** True when per-anchor alignment is active. */
  readonly aligned: boolean;
  /** Ref callback to register a card element by proposal id. */
  registerCard(id: string): (element: HTMLElement | null) => void;
  /** Ref callback for the scroll container the cards are positioned within. */
  registerContainer(element: HTMLElement | null): void;
}

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
  const { anchorRects, ids, gap } = options;
  const aligned = anchorRects !== undefined;

  const cardsRef = useRef(new Map<string, HTMLElement>());
  const containerRef = useRef<HTMLElement | null>(null);
  const lastPlacementRef = useRef<AlignPlacement[]>([]);
  const frameRef = useRef(0);
  // The id order is read imperatively during measurement, so keep it current
  // without re-subscribing the geometry listeners on every render.
  const idsRef = useRef<readonly string[]>(ids);
  idsRef.current = ids;

  const measure = useCallback(() => {
    const source = anchorRects;
    const container = containerRef.current;
    if (source === undefined || container === null) return;

    const inputs: AlignInput[] = [];
    for (const id of idsRef.current) {
      const element = cardsRef.current.get(id);
      if (element === undefined) continue;
      const rect = source.anchorRect(id);
      if (rect === null) continue;
      inputs.push({ id, anchorTop: rect.top, height: element.offsetHeight });
    }

    const placements = computeAlignedLayout(inputs, { gap });
    if (placementsEqual(placements, lastPlacementRef.current)) return;
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
  }, [anchorRects, gap]);

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
    };
  }, [aligned, anchorRects, requestMeasure, ids]);

  const registerCard = useCallback(
    (id: string) => (element: HTMLElement | null) => {
      if (element === null) {
        cardsRef.current.delete(id);
        return;
      }
      cardsRef.current.set(id, element);
      if (aligned) requestMeasure();
    },
    [aligned, requestMeasure],
  );

  const registerContainer = useCallback(
    (element: HTMLElement | null) => {
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
