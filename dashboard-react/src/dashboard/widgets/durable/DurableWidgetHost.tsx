import { useEffect, useMemo, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

import type { WidgetInstanceId } from "../../contributions/contracts";
import { DurableHostContext, type DurableHostHandle } from "./durableContext";

/** One live element the host keeps mounted across every cell remount. */
export interface DurableEntry {
  readonly instanceId: WidgetInstanceId;
  readonly node: ReactNode;
}

export interface DurableWidgetHostProps {
  readonly entries: readonly DurableEntry[];
  readonly children: ReactNode;
}

/**
 * DurableWidgetHost is the keep-alive layer that sits above the grid. For every
 * entry it owns one permanent wrapper div and portals the entry's live element
 * into that wrapper exactly once. The wrapper is a plain DOM node the host moves
 * by hand: a placeholder DurableCell pulls it into place on mount and parks it
 * in an offstage stash on unmount. Because the portal target never changes and
 * the element is never unmounted, its React state, its real DOM nodes, and any
 * focus inside it all survive a cell remount, a customize toggle, or an
 * interaction-recovery remount of the grid below.
 *
 * The wrapper divs are created lazily while rendering the portals. Allocating a
 * detached div is not a change to the on-screen document, so it is safe to do
 * during render and stays idempotent when React double-invokes render. Every
 * edit to the live document, meaning moving a wrapper into a cell, parking it in
 * the stash, evicting a departed wrapper, and restoring focus, happens only
 * inside ref callbacks and effects, never during render.
 */
export function DurableWidgetHost({
  entries,
  children,
}: DurableWidgetHostProps) {
  const wrappers = useRef<Map<WidgetInstanceId, HTMLDivElement>>(new Map());
  const stashRef = useRef<HTMLDivElement | null>(null);
  const focusRecords = useRef<Map<WidgetInstanceId, HTMLElement>>(new Map());

  // Get-or-create the permanent wrapper for an instance. Creating a detached div
  // is an allocation rather than an edit to the rendered document, so calling it
  // while rendering the portals below is both safe and repeatable.
  const wrapperFor = (instanceId: WidgetInstanceId): HTMLDivElement => {
    const existing = wrappers.current.get(instanceId);
    if (existing !== undefined) {
      return existing;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "wb-durable-slot";
    wrappers.current.set(instanceId, wrapper);
    return wrapper;
  };

  // A single stable handle so descendant cells never re-subscribe. Every closure
  // reads through refs, so the identity can stay fixed for the host's lifetime.
  const handle = useMemo<DurableHostHandle>(
    () => ({
      adopt(instanceId, cell) {
        const wrapper = wrappers.current.get(instanceId);
        if (wrapper === undefined) {
          return;
        }
        cell.appendChild(wrapper);
        // Restore focus only when a matching release actually captured it. A
        // first mount holds no record and must never grab focus on its own.
        const captured = focusRecords.current.get(instanceId);
        if (captured === undefined) {
          return;
        }
        focusRecords.current.delete(instanceId);
        // Moving the wrapper above blurs the captured element in a real browser.
        // Wait one microtask for the DOM to settle, then refocus only if that
        // element is still inside this wrapper and focus fell to the body or the
        // stash. If anything else has taken focus meanwhile, leave it be.
        queueMicrotask(() => {
          if (!wrapper.contains(captured)) {
            return;
          }
          const active = document.activeElement;
          const stash = stashRef.current;
          const focusIsUnclaimed =
            active === null ||
            active === document.body ||
            (stash !== null && stash.contains(active));
          if (focusIsUnclaimed) {
            captured.focus();
          }
        });
      },
      release(instanceId, cell) {
        const wrapper = wrappers.current.get(instanceId);
        if (wrapper === undefined) {
          return;
        }
        // Parent-identity guard: park the wrapper only if this exact cell still
        // holds it. Under StrictMode an adopt and release pair runs twice, and
        // this keeps the extra release from stealing a wrapper a live cell owns.
        if (wrapper.parentElement !== cell) {
          return;
        }
        // Record focus before the move, because appendChild clears it.
        const active = document.activeElement;
        if (active instanceof HTMLElement && wrapper.contains(active)) {
          focusRecords.current.set(instanceId, active);
        }
        const stash = stashRef.current;
        if (stash !== null) {
          stash.appendChild(wrapper);
        } else {
          wrapper.remove();
        }
      },
    }),
    [],
  );

  // Evict wrappers whose instance is no longer among the entries. React has
  // already unmounted the departed entry's portal by the time this runs, so all
  // that remains is the now-empty wrapper div to detach and forget.
  useEffect(() => {
    const liveIds = new Set(entries.map((entry) => entry.instanceId));
    for (const [instanceId, wrapper] of wrappers.current) {
      if (!liveIds.has(instanceId)) {
        wrapper.remove();
        wrappers.current.delete(instanceId);
        focusRecords.current.delete(instanceId);
      }
    }
  }, [entries]);

  return (
    <DurableHostContext.Provider value={handle}>
      {children}
      <div ref={stashRef} className="wb-durable-stash" aria-hidden inert />
      {entries.map((entry) =>
        createPortal(entry.node, wrapperFor(entry.instanceId), entry.instanceId),
      )}
    </DurableHostContext.Provider>
  );
}
