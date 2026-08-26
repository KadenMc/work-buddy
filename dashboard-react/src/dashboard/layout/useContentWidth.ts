import type { RefObject } from "react";
import { useContainerWidth } from "react-grid-layout";

/** Keep the measurement implementation private to the layout boundary. */
export function useContentWidth(): {
  readonly width: number;
  readonly ref: RefObject<HTMLDivElement | null>;
} {
  const { width, containerRef } = useContainerWidth({ initialWidth: 0 });
  return { width, ref: containerRef };
}
