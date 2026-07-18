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
        setNarrow(element.getBoundingClientRect().width < threshold);
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
