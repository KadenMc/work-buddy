/**
 * Report whether a container is narrower than a threshold, so the Review stream
 * can fall back to the grouped list where a margin cannot exist (SP-6 F6, gate
 * condition 12). It hands back a callback ref, so the ResizeObserver attaches
 * exactly when the measured node mounts (robust to conditional rendering), and
 * it is guarded for environments that lack a ResizeObserver.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type NarrowRef = (element: HTMLElement | null) => void;

export function useIsNarrow(threshold = 360): [boolean, NarrowRef] {
  const [narrow, setNarrow] = useState(false);
  const observerRef = useRef<ResizeObserver | null>(null);

  const setRef = useCallback<NarrowRef>(
    (element) => {
      observerRef.current?.disconnect();
      observerRef.current = null;
      if (element === null || typeof ResizeObserver !== "function") return;
      const measure = () => {
        const width = element.getBoundingClientRect().width;
        // A width of 0 means the element is not laid out yet (pre-paint, an
        // unmeasured jsdom node, or display:none). Only a real, positive
        // sub-threshold width is narrow, so the grouped fallback never flashes
        // before the first true measurement.
        setNarrow(width > 0 && width < threshold);
      };
      measure();
      const observer = new ResizeObserver(measure);
      observer.observe(element);
      observerRef.current = observer;
    },
    [threshold],
  );

  useEffect(
    () => () => {
      observerRef.current?.disconnect();
    },
    [],
  );

  return [narrow, setRef];
}
