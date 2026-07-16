import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DashboardEventProvider } from "../dashboard/events/DashboardEventProvider";
import { asSettingId } from "./contracts";
import { useSettingsValues } from "./useSettingsValues";

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly listeners = new Map<string, Set<EventListenerOrEventListenerObject>>();

  constructor(_url: string | URL) {
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    this.listeners.get(type)?.delete(listener);
  }

  close() {}

  message(payload: unknown) {
    const event = new MessageEvent("message", { data: JSON.stringify(payload) });
    this.listeners.get("message")?.forEach((listener) => {
      if (typeof listener === "function") listener(event);
      else listener.handleEvent(event);
    });
  }
}

function Probe() {
  const state = useSettingsValues("wb.settings.app.journal");
  const value = state.snapshot?.values.get(
    asSettingId("wb.journal.day-boundary"),
  );
  return <output>{`${state.status}:${String(value?.effectiveValue ?? "none")}`}</output>;
}

function MutationProbe() {
  const state = useSettingsValues("wb.settings.app.journal");
  const settingId = asSettingId("wb.journal.day-boundary");
  const value = state.snapshot?.values.get(settingId);
  return (
    <>
      <output>{`${value?.revision ?? "none"}:${String(value?.effectiveValue ?? "none")}`}</output>
      <button type="button" onClick={() => void state.write(settingId, "04:00")}>
        Write
      </button>
    </>
  );
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
});

afterEach(() => vi.unstubAllGlobals());

describe("useSettingsValues", () => {
  it("reconciles the current page through the host EventSource after settings.changed", async () => {
    let request = 0;
    const fetchMock = vi.fn(async () => {
      request += 1;
      return Response.json({
        schema_version: 1,
        registry_revision: "settings-registry:1",
        timezone: "America/Toronto",
        observed_at: "2026-07-15T12:00:00Z",
        read_only: false,
        values: [
          {
            setting_id: "wb.journal.day-boundary",
            scope: { kind: "profile", subject_id: "default" },
            effective_value: request === 1 ? "05:00" : "04:00",
            configured_value: request === 1 ? "05:00" : "04:00",
            source: request === 1 ? "default" : "profile",
            is_modified: request > 1,
            revision: `value:${request}`,
          },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <DashboardEventProvider>
        <Probe />
      </DashboardEventProvider>,
    );
    await screen.findByText("ready:05:00");
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));

    act(() =>
      MockEventSource.instances[0]!.message({
        event_type: "settings.changed",
        payload: {
          setting_ids: ["wb.journal.day-boundary"],
          value_revision: "value:2",
        },
        ts: 1_789_000_000,
      }),
    );

    await screen.findByText("ready:04:00");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not let a slower mutation response replace a newer SSE reconciliation", async () => {
    let read = 0;
    let resolvePatch: ((response: Response) => void) | undefined;
    const patchResponse = new Promise<Response>((resolve) => {
      resolvePatch = resolve;
    });
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") return patchResponse;
      read += 1;
      const latest = read > 1;
      return Response.json({
        schema_version: 1,
        registry_revision: "settings-registry:1",
        timezone: "America/Toronto",
        observed_at: "2026-07-15T12:00:00Z",
        read_only: false,
        values: [
          {
            setting_id: "wb.journal.day-boundary",
            scope: { kind: "profile", subject_id: "default" },
            effective_value: latest ? "03:00" : "05:00",
            configured_value: latest ? "03:00" : "05:00",
            source: latest ? "profile" : "default",
            is_modified: latest,
            revision: latest ? "value:2" : "value:0",
          },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <DashboardEventProvider>
        <MutationProbe />
      </DashboardEventProvider>,
    );
    await screen.findByText("value:0:05:00");
    fireEvent.click(screen.getByRole("button", { name: "Write" }));
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    act(() =>
      MockEventSource.instances[0]!.message({
        event_type: "settings.changed",
        payload: { setting_ids: ["wb.journal.day-boundary"] },
        ts: 1_789_000_000,
      }),
    );
    await screen.findByText("value:2:03:00");

    resolvePatch!(
      Response.json({
        schema_version: 1,
        registry_revision: "settings-registry:1",
        timezone: "America/Toronto",
        value: {
          setting_id: "wb.journal.day-boundary",
          scope: { kind: "profile", subject_id: "default" },
          effective_value: "04:00",
          configured_value: "04:00",
          source: "profile",
          is_modified: true,
          revision: "value:1",
        },
        event: { type: "settings.changed" },
      }),
    );

    await waitFor(() => expect(screen.getByText("value:2:03:00")).toBeInTheDocument());
    expect(screen.queryByText("value:1:04:00")).not.toBeInTheDocument();
  });
});
