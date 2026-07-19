import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

const STORAGE_KEY = "wb.cowork.rail-width";
// The prior fixed rail measured 20rem. Keeping it as the default means an
// untouched view looks exactly as before until the user drags the handle.
const DEFAULT_WIDTH = 320;
const MIN_WIDTH = 256;
const MAX_WIDTH = 720;
// Never let the rail grow so far that the editor becomes unusable.
const EDITOR_MIN = 320;
const KEY_STEP = 24;

const clampWidth = (value: number, bodyWidth: number | null): number => {
  const ceiling =
    bodyWidth !== null ? Math.min(MAX_WIDTH, bodyWidth - EDITOR_MIN) : MAX_WIDTH;
  const upper = Math.max(MIN_WIDTH, ceiling);
  return Math.round(Math.min(upper, Math.max(MIN_WIDTH, value)));
};

const readStoredWidth = (): number => {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw === null ? Number.NaN : Number.parseInt(raw, 10);
    return Number.isFinite(parsed) ? parsed : DEFAULT_WIDTH;
  } catch {
    return DEFAULT_WIDTH;
  }
};

const storeWidth = (value: number): void => {
  try {
    window.localStorage.setItem(STORAGE_KEY, String(value));
  } catch {
    // Persistence is best-effort. A blocked storage never breaks resizing.
  }
};

export interface RailSeparatorProps {
  readonly role: "separator";
  readonly "aria-orientation": "vertical";
  readonly "aria-label": string;
  readonly "aria-valuenow": number;
  readonly "aria-valuemin": number;
  readonly "aria-valuemax": number;
  readonly tabIndex: 0;
  readonly onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  readonly onKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
}

export interface ResizableRail {
  readonly width: number;
  readonly bodyRef: (element: HTMLDivElement | null) => void;
  readonly separatorProps: RailSeparatorProps;
}

/**
 * Drives a draggable width for the Co-work review rail. The rail sits on the
 * inline-end, so moving the separator toward the editor (a smaller clientX)
 * widens it. Width is clamped so the editor keeps a usable minimum, persisted
 * to localStorage, and re-clamped when the window shrinks.
 */
export function useResizableRail(): ResizableRail {
  const bodyElementRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState<number>(() =>
    clampWidth(readStoredWidth(), null),
  );
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const measuredBodyWidth = (): number | null =>
    bodyElementRef.current?.getBoundingClientRect().width ?? null;

  const commit = useCallback((next: number) => {
    setWidth(next);
    storeWidth(next);
  }, []);

  const onPointerMove = useCallback((event: PointerEvent) => {
    const drag = dragRef.current;
    if (drag === null) return;
    const delta = drag.startX - event.clientX;
    setWidth(clampWidth(drag.startWidth + delta, measuredBodyWidth()));
  }, []);

  const endDrag = useCallback(() => {
    if (dragRef.current === null) return;
    dragRef.current = null;
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", endDrag);
    document.body.classList.remove("wb-cowork-resizing");
    setWidth((current) => {
      storeWidth(current);
      return current;
    });
  }, [onPointerMove]);

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      dragRef.current = { startX: event.clientX, startWidth: width };
      document.body.classList.add("wb-cowork-resizing");
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", endDrag);
    },
    [width, onPointerMove, endDrag],
  );

  const onKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      const measured = measuredBodyWidth();
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        commit(clampWidth(width + KEY_STEP, measured));
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        commit(clampWidth(width - KEY_STEP, measured));
      } else if (event.key === "Home") {
        event.preventDefault();
        commit(clampWidth(MAX_WIDTH, measured));
      } else if (event.key === "End") {
        event.preventDefault();
        commit(clampWidth(MIN_WIDTH, measured));
      }
    },
    [width, commit],
  );

  useEffect(() => {
    const reclamp = () =>
      setWidth((current) => clampWidth(current, measuredBodyWidth()));
    reclamp();
    window.addEventListener("resize", reclamp);
    return () => {
      window.removeEventListener("resize", reclamp);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", endDrag);
    };
  }, [onPointerMove, endDrag]);

  const bodyRef = useCallback((element: HTMLDivElement | null) => {
    bodyElementRef.current = element;
  }, []);

  return {
    width,
    bodyRef,
    separatorProps: {
      role: "separator",
      "aria-orientation": "vertical",
      "aria-label": "Resize the review panel",
      "aria-valuenow": width,
      "aria-valuemin": MIN_WIDTH,
      "aria-valuemax": MAX_WIDTH,
      tabIndex: 0,
      onPointerDown,
      onKeyDown,
    },
  };
}
