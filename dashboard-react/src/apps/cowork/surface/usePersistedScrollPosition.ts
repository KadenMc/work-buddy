import { useCallback, useEffect, useRef, type RefCallback } from "react";

export type CoworkScrollSurface = "editor" | "review";

const STORAGE_PREFIX = "wb.cowork.scroll-position.v1";
const DEFAULT_WRITE_DELAY_MS = 250;
const DEFAULT_RESTORE_SETTLE_MS = 120;
const DEFAULT_RESTORE_DEADLINE_MS = 15_000;
const SCROLL_KEYS = new Set([
  "ArrowDown",
  "ArrowUp",
  "End",
  "Home",
  "PageDown",
  "PageUp",
  " ",
]);

interface StoredScrollPosition {
  readonly version: 1;
  readonly top: number;
}

export interface CoworkScrollIdentity {
  readonly documentId: string;
  readonly storeId?: string;
}

export interface PersistedScrollPositionOptions {
  readonly key: string;
  /** Injectable for tests; `undefined` resolves to localStorage and `null` disables persistence. */
  readonly storage?: Storage | null;
  readonly writeDelayMs?: number;
  readonly restoreSettleMs?: number;
  readonly restoreDeadlineMs?: number;
}

/**
 * Scroll state is device-local UI state, not document state. Live documents include their
 * store id so identical document ids in different stores cannot share a position. Demo and
 * scratch documents have no store and deliberately use the explicit document-only namespace.
 */
export function coworkScrollPositionStorageKey(
  identity: CoworkScrollIdentity,
  surface: CoworkScrollSurface,
): string {
  const store =
    identity.storeId === undefined || identity.storeId.length === 0
      ? "document-only"
      : `store:${encodeURIComponent(identity.storeId)}`;
  return `${STORAGE_PREFIX}:${store}:document:${encodeURIComponent(identity.documentId)}:${surface}`;
}

export function loadCoworkScrollPosition(
  storage: Storage,
  key: string,
): number | null {
  try {
    const raw = storage.getItem(key);
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as Partial<StoredScrollPosition>;
    if (
      parsed.version !== 1 ||
      typeof parsed.top !== "number" ||
      !Number.isFinite(parsed.top) ||
      parsed.top < 0
    ) {
      return null;
    }
    return parsed.top;
  } catch {
    // Storage can be unavailable or quota/privacy blocked. Scroll remains functional.
    return null;
  }
}

export function saveCoworkScrollPosition(
  storage: Storage,
  key: string,
  top: number,
): void {
  if (!Number.isFinite(top)) return;
  const value: StoredScrollPosition = {
    version: 1,
    top: Math.max(0, top),
  };
  try {
    storage.setItem(key, JSON.stringify(value));
  } catch {
    // Persistence is best-effort UI continuity; never make the scroll surface unusable.
  }
}

const resolveStorage = (storage: Storage | null | undefined): Storage | null => {
  if (storage !== undefined) return storage;
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

/**
 * Owns one attached scroll container. A saved offset may be deeper than the loading shell, so
 * restoration remains pending while editor hydration or Review data grows the scroll range.
 * The first genuine user scroll intent cancels that pending work; late content can therefore
 * never snap an already-interacting user back to the saved position.
 */
class PersistedScrollPositionBinding {
  readonly #element: HTMLElement;
  readonly #storage: Storage;
  readonly #key: string;
  readonly #window: Window;
  readonly #writeDelayMs: number;
  readonly #restoreSettleMs: number;
  readonly #restoreDeadlineMs: number;
  #desiredTop: number | null;
  #restoring: boolean;
  #dirty = false;
  #writeTimer: number | null = null;
  #settleTimer: number | null = null;
  #deadlineTimer: number | null = null;
  #restoreAssignedTop: number | null = null;
  #mutationObserver: MutationObserver | null = null;
  #resizeObserver: ResizeObserver | null = null;

  constructor(
    element: HTMLElement,
    storage: Storage,
    options: Required<
      Pick<
        PersistedScrollPositionOptions,
        "key" | "writeDelayMs" | "restoreSettleMs" | "restoreDeadlineMs"
      >
    >,
  ) {
    this.#element = element;
    this.#storage = storage;
    this.#key = options.key;
    this.#window = element.ownerDocument.defaultView ?? window;
    this.#writeDelayMs = options.writeDelayMs;
    this.#restoreSettleMs = options.restoreSettleMs;
    this.#restoreDeadlineMs = options.restoreDeadlineMs;
    this.#desiredTop = loadCoworkScrollPosition(storage, options.key);
    this.#restoring = this.#desiredTop !== null;
  }

  start(): void {
    this.#element.addEventListener("scroll", this.#onScroll, { passive: true });
    this.#element.addEventListener("wheel", this.#onUserIntent, { passive: true });
    this.#element.addEventListener("touchmove", this.#onUserIntent, {
      passive: true,
    });
    this.#element.addEventListener("keydown", this.#onKeyDown);
    this.#window.addEventListener("pagehide", this.#onPageHide);
    this.#element.ownerDocument.addEventListener(
      "visibilitychange",
      this.#onVisibilityChange,
    );

    if (!this.#restoring) return;

    this.#mutationObserver = new MutationObserver(this.#onGeometryChanged);
    this.#mutationObserver.observe(this.#element, {
      attributes: true,
      childList: true,
      characterData: true,
      subtree: true,
    });
    if (typeof ResizeObserver !== "undefined") {
      this.#resizeObserver = new ResizeObserver(this.#onGeometryChanged);
      this.#resizeObserver.observe(this.#element);
      this.#observeContentSize();
    }
    this.#deadlineTimer = this.#window.setTimeout(
      this.#finishAtSafeClamp,
      this.#restoreDeadlineMs,
    );
    this.#attemptRestore();
  }

  dispose(): void {
    this.#element.removeEventListener("scroll", this.#onScroll);
    this.#element.removeEventListener("wheel", this.#onUserIntent);
    this.#element.removeEventListener("touchmove", this.#onUserIntent);
    this.#element.removeEventListener("keydown", this.#onKeyDown);
    this.#window.removeEventListener("pagehide", this.#onPageHide);
    this.#element.ownerDocument.removeEventListener(
      "visibilitychange",
      this.#onVisibilityChange,
    );
    this.#prepareLifecycleFlush();
    this.#stopRestoration();
    if (this.#writeTimer !== null) this.#window.clearTimeout(this.#writeTimer);
    this.#writeTimer = null;
    this.#flushDirtyPosition();
  }

  #maxScrollTop(): number {
    return Math.max(0, this.#element.scrollHeight - this.#element.clientHeight);
  }

  #observeContentSize(): void {
    if (this.#resizeObserver === null) return;
    const content = this.#element.firstElementChild;
    if (content instanceof Element) this.#resizeObserver.observe(content);
  }

  #attemptRestore = (): void => {
    if (!this.#restoring || this.#desiredTop === null) return;
    const max = this.#maxScrollTop();
    this.#assignRestoredTop(Math.min(this.#desiredTop, max));
    this.#observeContentSize();

    if (max + 0.5 < this.#desiredTop) return;
    if (this.#settleTimer !== null) this.#window.clearTimeout(this.#settleTimer);
    this.#settleTimer = this.#window.setTimeout(() => {
      if (!this.#restoring || this.#desiredTop === null) return;
      const settledMax = this.#maxScrollTop();
      if (settledMax + 0.5 < this.#desiredTop) {
        this.#attemptRestore();
        return;
      }
      this.#assignRestoredTop(Math.min(this.#desiredTop, settledMax));
      this.#stopRestoration();
    }, this.#restoreSettleMs);
  };

  #onGeometryChanged = (): void => {
    this.#attemptRestore();
  };

  #stopRestoration(): void {
    this.#restoring = false;
    this.#desiredTop = null;
    this.#mutationObserver?.disconnect();
    this.#mutationObserver = null;
    this.#resizeObserver?.disconnect();
    this.#resizeObserver = null;
    if (this.#settleTimer !== null) this.#window.clearTimeout(this.#settleTimer);
    this.#settleTimer = null;
    if (this.#deadlineTimer !== null) this.#window.clearTimeout(this.#deadlineTimer);
    this.#deadlineTimer = null;
  }

  #finishAtSafeClamp = (): void => {
    if (!this.#restoring || this.#desiredTop === null) return;
    this.#assignRestoredTop(
      Math.min(this.#desiredTop, this.#maxScrollTop()),
    );
    this.#stopRestoration();
    this.#dirty = true;
    this.#flushDirtyPosition();
  };

  #assignRestoredTop(top: number): void {
    // Scroll events from assignments can be delivered asynchronously. Remember the exact
    // value so only this binding's own event is suppressed; an editor anchor jump or any other
    // external programmatic scroll is treated as a new user position and cancels restoration.
    this.#restoreAssignedTop = top;
    this.#element.scrollTop = top;
  }

  #onScroll = (): void => {
    if (
      this.#restoreAssignedTop !== null &&
      Math.abs(this.#element.scrollTop - this.#restoreAssignedTop) <= 0.5
    ) {
      // Keep the assignment while restoration is pending so a route change can still tell
      // this temporary clamp from an external jump whose scroll event has not fired yet.
      if (!this.#restoring) this.#restoreAssignedTop = null;
      return;
    }
    this.#restoreAssignedTop = null;
    if (this.#restoring) {
      this.#stopRestoration();
    }
    this.#dirty = true;
    this.#scheduleWrite();
  };

  #onUserIntent = (): void => {
    if (!this.#restoring) return;
    this.#stopRestoration();
    this.#restoreAssignedTop = null;
    this.#dirty = true;
    this.#scheduleWrite();
  };

  #onKeyDown = (event: KeyboardEvent): void => {
    // Descendant controls and the rich-text editor use these keys for activation,
    // selection, and caret movement. Their eventual scroll event still wins; only
    // keyboard scrolling directed at the container itself is advance intent.
    if (event.target === this.#element && SCROLL_KEYS.has(event.key)) {
      this.#onUserIntent();
    }
  };

  #scheduleWrite(): void {
    if (this.#writeTimer !== null) this.#window.clearTimeout(this.#writeTimer);
    this.#writeTimer = this.#window.setTimeout(() => {
      this.#writeTimer = null;
      this.#flushDirtyPosition();
    }, this.#writeDelayMs);
  }

  #flushDirtyPosition(): void {
    if (!this.#dirty || this.#restoring) return;
    this.#dirty = false;
    saveCoworkScrollPosition(this.#storage, this.#key, this.#element.scrollTop);
  }

  #prepareLifecycleFlush(): void {
    if (this.#restoring) {
      const stillAtOwnAssignment =
        this.#restoreAssignedTop !== null &&
        Math.abs(this.#element.scrollTop - this.#restoreAssignedTop) <= 0.5;
      // Do not replace a deep saved offset with the loading shell's temporary clamp. If the
      // element moved elsewhere, however, an explicit/user/programmatic navigation won.
      if (stillAtOwnAssignment) return;
      this.#stopRestoration();
    }
    this.#dirty = true;
  }

  #onPageHide = (): void => {
    this.#prepareLifecycleFlush();
    this.#flushDirtyPosition();
  };

  #onVisibilityChange = (): void => {
    if (this.#element.ownerDocument.visibilityState === "hidden") {
      this.#prepareLifecycleFlush();
      this.#flushDirtyPosition();
    }
  };
}

/**
 * Returns a callback ref for a vertically scrolling element. Writes are throttled, flushed on
 * navigation/unmount/page hide, and suppressed for the entire asynchronous restore window.
 */
export function usePersistedScrollPosition({
  key,
  storage,
  writeDelayMs = DEFAULT_WRITE_DELAY_MS,
  restoreSettleMs = DEFAULT_RESTORE_SETTLE_MS,
  restoreDeadlineMs = DEFAULT_RESTORE_DEADLINE_MS,
}: PersistedScrollPositionOptions): RefCallback<HTMLElement> {
  const bindingRef = useRef<PersistedScrollPositionBinding | null>(null);

  const attach = useCallback<RefCallback<HTMLElement>>(
    (element) => {
      bindingRef.current?.dispose();
      bindingRef.current = null;
      const resolvedStorage = resolveStorage(storage);
      if (element === null || resolvedStorage === null) return;
      const binding = new PersistedScrollPositionBinding(element, resolvedStorage, {
        key,
        writeDelayMs,
        restoreSettleMs,
        restoreDeadlineMs,
      });
      bindingRef.current = binding;
      binding.start();
    },
    [key, restoreDeadlineMs, restoreSettleMs, storage, writeDelayMs],
  );

  useEffect(
    () => () => {
      bindingRef.current?.dispose();
      bindingRef.current = null;
    },
    [],
  );

  return attach;
}
