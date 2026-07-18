import type { AdapterEvents } from "./types";

type Listener<K extends keyof AdapterEvents> = (payload: AdapterEvents[K]) => void;

/**
 * A tiny typed event surface for the Review rail (C1 surface section 3, AdapterEvents).
 * The rail subscribes to proposal-set and anchor lifecycle events and never touches
 * ProseMirror directly. Handlers are held per event name, `on` returns an unsubscribe
 * thunk, and `emit` is synchronous so a decoration rebuild lands in the same frame.
 */
export class AdapterEventBus {
  readonly #listeners = new Map<keyof AdapterEvents, Set<Listener<keyof AdapterEvents>>>();

  on<K extends keyof AdapterEvents>(ev: K, cb: (payload: AdapterEvents[K]) => void): () => void {
    const set = this.#listeners.get(ev) ?? new Set();
    set.add(cb as Listener<keyof AdapterEvents>);
    this.#listeners.set(ev, set);
    return () => {
      set.delete(cb as Listener<keyof AdapterEvents>);
    };
  }

  emit<K extends keyof AdapterEvents>(ev: K, payload: AdapterEvents[K]): void {
    const set = this.#listeners.get(ev);
    if (set === undefined) return;
    for (const cb of [...set]) {
      (cb as Listener<K>)(payload);
    }
  }

  /** Drop every listener, called on adapter detach so a rebound editor starts clean. */
  clear(): void {
    this.#listeners.clear();
  }
}
