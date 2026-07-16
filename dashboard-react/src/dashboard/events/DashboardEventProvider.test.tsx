import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Header from "../../components/Header";
import { ThemeProvider } from "../../theme/ThemeProvider";
import {
  DashboardEventProvider,
  normalizeDashboardEvent,
  useDashboardEvents,
} from "./DashboardEventProvider";

class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  readonly listeners = new Map<string, Set<EventListenerOrEventListenerObject>>();
  closed = false;

  constructor(url: string | URL) {
    this.url = String(url);
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

  close(): void {
    this.closed = true;
  }

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

function EventProbe({ testId }: { readonly testId: string }) {
  const events = useDashboardEvents();
  return (
    <output data-testid={testId}>
      {JSON.stringify({
        connection: events.connectionState,
        invalidation: events.lastInvalidation?.invalidation.reason,
        reconcile: events.reconcileSignal?.reason,
      })}
    </output>
  );
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => ({ status: "running" }) })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("DashboardEventProvider", () => {
  it("owns one EventSource for Header and every other consumer", async () => {
    const rendered = render(
      <ThemeProvider initialPreference={{ scheme: "dark", skinId: "wb.default" }}>
        <DashboardEventProvider>
          <Header />
          <EventProbe testId="first" />
          <EventProbe testId="second" />
        </DashboardEventProvider>
      </ThemeProvider>,
    );

    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    const source = MockEventSource.instances[0]!;
    expect(source.url).toBe("/api/events");

    act(() => source.emit("open"));

    expect(rendered.container.querySelector(".bus-status")?.textContent).toContain("live");
    expect(screen.getByTestId("first")).toHaveTextContent('"connection":"live"');
    expect(screen.getByTestId("second")).toHaveTextContent('"connection":"live"');

    rendered.unmount();
    expect(source.closed).toBe(true);
  });

  it("normalizes data events, ignores heartbeat/malformed frames, and signals reconnect", async () => {
    render(
      <DashboardEventProvider>
        <EventProbe testId="events" />
      </DashboardEventProvider>,
    );
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    const source = MockEventSource.instances[0]!;

    act(() => {
      source.emit("open");
      source.emit(
        "message",
        JSON.stringify({ event_type: "bus.heartbeat", payload: {}, ts: 1 }),
      );
      source.emit("message", "not json");
    });
    expect(screen.getByTestId("events")).not.toHaveTextContent("invalidation");

    act(() =>
      source.emit(
        "message",
        JSON.stringify({
          event_type: "task.created",
          payload: { app_id: "wb.tasks", revision: 3 },
          ts: 1_789_000_000,
        }),
      ),
    );
    expect(screen.getByTestId("events")).toHaveTextContent("task.created");

    act(() => {
      source.emit("error");
      source.emit("open");
    });
    expect(screen.getByTestId("events")).toHaveTextContent('"reconcile":"reconnected"');
  });
});

describe("normalizeDashboardEvent", () => {
  it("accepts CloudEvents-shaped invalidations and preserves routing metadata", () => {
    expect(
      normalizeDashboardEvent(
        {
          specversion: "1.0",
          id: "event-7",
          source: "/apps/wb.journal",
          type: "ai.workbuddy.journal.changed",
          time: "2026-07-12T12:00:00Z",
          data: { view_ids: ["wb.journal.main"], revision: "r7" },
        },
        "fallback",
      ),
    ).toEqual({
      id: "event-7",
      appId: "wb.journal",
      viewIds: ["wb.journal.main"],
      revision: "r7",
      reason: "ai.workbuddy.journal.changed",
      observedAt: "2026-07-12T12:00:00Z",
    });
  });
});
