/**
 * The selector hook over a RailStore, the useEditorState pattern. A component
 * subscribes to a narrow slice through a selector and re-renders only when that
 * slice changes by the given equality. The selection is cached in a ref so an
 * object-returning selector does not tear or loop under useSyncExternalStore.
 */

import { useCallback, useRef, useSyncExternalStore } from "react";

import type { RailState } from "./store";
import type { RailStore } from "./store";

function strictEqual<T>(a: T, b: T): boolean {
  return Object.is(a, b);
}

export function useRailState<T>(
  store: RailStore,
  selector: (state: RailState) => T,
  isEqual: (a: T, b: T) => boolean = strictEqual,
): T {
  const lastRef = useRef<{ value: T } | null>(null);

  const getSelection = useCallback((): T => {
    const next = selector(store.getState());
    const last = lastRef.current;
    if (last !== null && isEqual(last.value, next)) {
      return last.value;
    }
    lastRef.current = { value: next };
    return next;
  }, [store, selector, isEqual]);

  return useSyncExternalStore(store.subscribe, getSelection, getSelection);
}

/** Shallow-equal for the small record and array slices the rail selects. */
export function shallowArrayEqual<T>(
  a: readonly T[],
  b: readonly T[],
): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let index = 0; index < a.length; index += 1) {
    if (!Object.is(a[index], b[index])) return false;
  }
  return true;
}
