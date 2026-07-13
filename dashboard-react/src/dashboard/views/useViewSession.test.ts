import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createElement, type ReactNode } from "react";

import {
  asAppId,
  asViewId,
  type AppInvalidation,
  type DashboardIntent,
  type ViewId,
  type ViewSnapshot,
} from "../contributions/contracts";
import { DashboardEventProvider } from "../events/DashboardEventProvider";
import type { ViewProvider } from "../providers/ViewProvider";
import { useViewSession } from "./useViewSession";

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly listeners = new Map<string, Set<EventListenerOrEventListenerObject>>();

  constructor(_url: string | URL) {
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {}

  emit(type: "open" | "error"): void;
  emit(type: "message", data: string): void;
  emit(type: "open" | "error" | "message", data?: string): void {
    const event =
      type === "message"
        ? new MessageEvent<string>("message", { data })
        : new Event(type);
    this.listeners.get(type)?.forEach((listener) => {
      if (typeof listener === "function") listener(event);
      else listener.handleEvent(event);
    });
  }
}

const appId = asAppId("example.session");
const firstViewId = asViewId("example.session.first");
const secondViewId = asViewId("example.session.second");

const snapshot = (viewId: ViewId, revision: number, title: string): ViewSnapshot => ({
  viewId,
  revision,
  observedAt: "2026-07-12T12:00:00Z",
  status: "ready",
  quality: { kind: "demo" },
  model: { title },
  bindings: {},
  widgetInputs: {},
});

const wrapper = ({ children }: { readonly children: ReactNode }) =>
  createElement(DashboardEventProvider, null, children);

const deferred = <Value,>() => {
  let resolve!: (value: Value) => void;
  const promise = new Promise<Value>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
});

describe("useViewSession", () => {
  it("suppresses an older load result after navigation starts a newer request", async () => {
    const first = deferred<ViewSnapshot>();
    const second = deferred<ViewSnapshot>();
    const provider: ViewProvider = {
      appId,
      loadView: vi.fn((viewId) =>
        viewId === firstViewId ? first.promise : second.promise,
      ),
      loadWidget: vi.fn(),
      dispatch: vi.fn(),
      reconcile: vi.fn(async () => ({ changed: false })),
    };
    const rendered = renderHook(
      ({ viewId }) => useViewSession({ provider, viewId }),
      { initialProps: { viewId: firstViewId }, wrapper },
    );

    await waitFor(() => expect(provider.loadView).toHaveBeenCalledWith(firstViewId, expect.anything()));
    rendered.rerender({ viewId: secondViewId });
    await waitFor(() => expect(provider.loadView).toHaveBeenCalledWith(secondViewId, expect.anything()));

    act(() => second.resolve(snapshot(secondViewId, 2, "Second")));
    await waitFor(() => expect(rendered.result.current.snapshot?.viewId).toBe(secondViewId));
    act(() => first.resolve(snapshot(firstViewId, 1, "First")));

    await waitFor(() =>
      expect(rendered.result.current.snapshot?.viewId).toBe(secondViewId),
    );
  });

  it("reconciles on relevant invalidations, reconnect, and foreground return", async () => {
    const reconcile = vi.fn(async (_invalidation: AppInvalidation) => ({
      changed: false,
    }));
    const provider: ViewProvider = {
      appId,
      loadView: vi.fn(async () => snapshot(firstViewId, 1, "First")),
      loadWidget: vi.fn(),
      dispatch: vi.fn(),
      reconcile,
    };
    const rendered = renderHook(() => useViewSession({ provider, viewId: firstViewId }), {
      wrapper,
    });
    await waitFor(() => expect(rendered.result.current.status).toBe("ready"));
    const source = MockEventSource.instances[0]!;

    act(() => source.emit("open"));
    await waitFor(() => expect(reconcile).toHaveBeenCalledTimes(1));
    expect(reconcile.mock.calls[0]?.[0].reason).toBe("dashboard-connected");

    act(() =>
      source.emit(
        "message",
        JSON.stringify({
          event_type: "example.changed",
          payload: { app_id: appId, view_ids: [firstViewId], revision: 1 },
          ts: 1,
        }),
      ),
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(reconcile).toHaveBeenCalledTimes(1);

    act(() => {
      source.emit("error");
      source.emit("open");
    });
    await waitFor(() => expect(reconcile).toHaveBeenCalledTimes(2));
    expect(reconcile.mock.calls[1]?.[0].reason).toBe("dashboard-reconnected");

    const visibility = vi
      .spyOn(document, "visibilityState", "get")
      .mockReturnValue("visible");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(reconcile).toHaveBeenCalledTimes(3));
    expect(reconcile.mock.calls[2]?.[0].reason).toBe("dashboard-foreground");
    visibility.mockRestore();
  });

  it("dispatches through the provider and adopts its reconciled revision", async () => {
    const provider: ViewProvider = {
      appId,
      loadView: vi.fn(async () => snapshot(firstViewId, 1, "Before")),
      loadWidget: vi.fn(),
      dispatch: vi.fn(async (intent: DashboardIntent) => ({
        intent_id: intent.intent_id,
        status: "accepted" as const,
        revision: 2,
      })),
      reconcile: vi.fn(async () => ({
        changed: true,
        revision: 2,
        snapshot: snapshot(firstViewId, 2, "After"),
      })),
    };
    const rendered = renderHook(() => useViewSession({ provider, viewId: firstViewId }), {
      wrapper,
    });
    await waitFor(() => expect(rendered.result.current.status).toBe("ready"));

    await act(async () => {
      await rendered.result.current.dispatch({
        intent_type: "example.session.update",
        schema_version: 1,
        intent_id: "intent-1",
        client_mutation_id: "mutation-1",
        view_id: firstViewId,
        payload: { title: "After" },
      });
    });

    expect(provider.dispatch).toHaveBeenCalledOnce();
    expect(provider.reconcile).toHaveBeenCalledOnce();
    expect(rendered.result.current.snapshot?.revision).toBe(2);
    expect(rendered.result.current.pendingIntentIds).toEqual([]);
  });
});
