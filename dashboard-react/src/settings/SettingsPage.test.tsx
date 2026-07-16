import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  TYPOGRAPHY_SCALE_STORAGE_KEY,
  TypographyScaleProvider,
} from "../theme/TypographyScaleProvider";
import { DashboardEventProvider } from "../dashboard/events/DashboardEventProvider";
import {
  formatInstantInTimeZone,
  SettingsPage,
  settingsFocusScrollBehavior,
} from "./SettingsPage";
import { nativeSettingsRegistry } from "./nativeContributions";

const notFoundFetch = vi.fn(async () => new Response(null, { status: 404 }));

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

describe("SettingsPage", () => {
  beforeEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset.wbTypeScale;
    vi.stubGlobal("fetch", notFoundFetch);
    vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
    MockEventSource.instances = [];
    notFoundFetch.mockClear();
  });

  afterEach(() => vi.unstubAllGlobals());

  it("offers a persistent discrete system-wide text-size control", () => {
    render(
      <MemoryRouter initialEntries={["/settings/system/accessibility"]}>
        <TypographyScaleProvider initialScale="standard">
          <SettingsPage registryOverride={nativeSettingsRegistry} />
        </TypographyScaleProvider>
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("heading", { name: "Accessibility" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Accessibility" }),
    ).toHaveClass("is-active");

    const slider = screen.getByRole("slider", { name: "Text size" });
    expect(slider).toHaveAttribute("aria-valuetext", "Standard, 100%");
    fireEvent.change(slider, { target: { value: "2" } });

    expect(slider).toHaveAttribute("aria-valuetext", "Extra large, 125%");
    expect(document.documentElement.dataset.wbTypeScale).toBe("extra-large");
    expect(localStorage.getItem(TYPOGRAPHY_SCALE_STORAGE_KEY)).toBe(
      "extra-large",
    );

    fireEvent.click(screen.getByRole("button", { name: "Reset to standard" }));
    expect(slider).toHaveAttribute("aria-valuetext", "Standard, 100%");
  });

  it("provides an explicit route back to the originating dashboard view", () => {
    render(
      <MemoryRouter
        initialEntries={[
          {
            pathname: "/settings/system/accessibility",
            state: { settingsReturnTo: "/journal?day=2026-07-11" },
          },
        ]}
      >
        <TypographyScaleProvider initialScale="standard">
          <Routes>
            <Route
              path="/settings/system/accessibility"
              element={
                <SettingsPage
                  defaultViewPath="/journal"
                  registryOverride={nativeSettingsRegistry}
                />
              }
            />
            <Route path="/journal" element={<h1>Journal return target</h1>} />
          </Routes>
        </TypographyScaleProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Back to dashboard" }));
    expect(
      screen.getByRole("heading", { name: "Journal return target" }),
    ).toBeInTheDocument();
  });

  it("returns one canonical setting from global search", async () => {
    render(
      <MemoryRouter initialEntries={["/settings/system/accessibility"]}>
        <TypographyScaleProvider initialScale="standard">
          <SettingsPage registryOverride={nativeSettingsRegistry} />
        </TypographyScaleProvider>
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByRole("searchbox", { name: "Search all settings" }), {
      target: { value: "day boundary" },
    });

    const results = screen.getByRole("region", { name: "Search results" });
    expect(results.querySelectorAll("li")).toHaveLength(1);
    expect(screen.getByRole("link", { name: /Journal day starts at/ })).toHaveAttribute(
      "href",
      "/settings/apps/journal?setting=wb.journal.day-boundary",
    );
  });

  it("filters sections within the active settings page without a server round trip", () => {
    render(
      <MemoryRouter initialEntries={["/settings/apps/journal"]}>
        <TypographyScaleProvider initialScale="standard">
          <SettingsPage registryOverride={nativeSettingsRegistry} />
        </TypographyScaleProvider>
      </MemoryRouter>,
    );

    const pageSearch = screen.getByRole("searchbox", {
      name: "Search within Journal settings",
    });
    fireEvent.change(pageSearch, { target: { value: "font" } });
    expect(screen.queryByRole("heading", { name: "Journal day starts at" })).not.toBeInTheDocument();
    expect(screen.getByText("0 settings")).toBeInTheDocument();

    fireEvent.change(pageSearch, { target: { value: "late night" } });
    expect(screen.getByRole("heading", { name: "Journal day starts at" })).toBeInTheDocument();
    expect(screen.getByText("1 setting")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Clear Journal settings search" }),
    );
    expect(pageSearch).toHaveValue("");
  });

  it("writes the Journal setting through the authoritative broker and shows pending state", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/settings/registry") {
        return new Response(null, { status: 404 });
      }
      if (url.startsWith("/api/settings/values?")) {
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
              effective_value: "05:00",
              configured_value: "05:00",
              source: "default",
              is_modified: false,
              revision: "value:0",
              pending_value: null,
              effective_at: null,
              apply_status: "effective",
              impact_preview: { timezone: "America/Toronto" },
            },
          ],
        });
      }
      if (url.endsWith("/api/settings/values/wb.journal.day-boundary/preview")) {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({
          scope: "profile",
          value: "04:00",
          expected_revision: "value:0",
        });
        return Response.json({
          schema_version: 1,
          registry_revision: "settings-registry:1",
          timezone: "America/Toronto",
          configured_timezone: "America/Toronto",
          value_revision: "value:0",
          preview: {
            setting_id: "wb.journal.day-boundary",
            scope: { kind: "profile", subject_id: "default" },
            value: "04:00",
            effective_at: "2026-07-16T05:00:00-04:00",
            apply_status: "pending",
            impact_preview: {
              timezone: "America/Toronto",
              current_day: {
                window_end: "2026-07-16T05:00:00-04:00",
              },
              pending_day: {
                window_start: "2026-07-16T04:00:00-04:00",
                window_end: "2026-07-17T04:00:00-04:00",
              },
            },
          },
          diagnostics: [],
        });
      }
      if (url.includes("/api/settings/values/wb.journal.day-boundary")) {
        expect(init?.method).toBe("PATCH");
        expect(JSON.parse(String(init?.body))).toEqual({
          scope: "profile",
          value: "04:00",
          expected_revision: "value:0",
        });
        return Response.json({
          schema_version: 1,
          registry_revision: "settings-registry:1",
          timezone: "America/Toronto",
          value: {
            setting_id: "wb.journal.day-boundary",
            scope: { kind: "profile", subject_id: "default" },
            effective_value: "05:00",
            configured_value: "04:00",
            source: "default",
            is_modified: true,
            revision: "value:1",
            pending_value: "04:00",
            effective_at: "2026-07-16T04:00:00-04:00",
            apply_status: "pending",
            impact_preview: { timezone: "America/Toronto" },
          },
          event: { type: "settings.changed" },
        });
      }
      return new Response(null, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/settings/apps/journal"]}>
        <TypographyScaleProvider initialScale="standard">
          <SettingsPage registryOverride={nativeSettingsRegistry} />
        </TypographyScaleProvider>
      </MemoryRouter>,
    );

    const input = await screen.findByLabelText("Day starts");
    await waitFor(() => expect(input).toBeEnabled());
    fireEvent.change(input, { target: { value: "04:00" } });
    expect(screen.getByRole("button", { name: "Save change" })).toBeDisabled();
    expect(
      await screen.findByRole("region", { name: "Unsaved change preview" }),
    ).toHaveTextContent("Preview · not saved");
    expect(
      screen.getByRole("region", { name: "Unsaved change preview" }),
    ).toHaveTextContent(/first Journal window/i);
    fireEvent.click(screen.getByRole("button", { name: "Save change" }));

    expect(
      await screen.findByText(/saved and will become effective/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/America\/Toronto/)).toBeInTheDocument();
    expect(screen.getByText(/Saved\. The change is pending/i)).toHaveAttribute(
      "role",
      "status",
    );
  });

  it("resolves canonical setting links to one preferred placement and preserves unknown IDs", async () => {
    const rendered = render(
      <MemoryRouter
        initialEntries={[
          {
            pathname: "/settings/setting/wb.journal.day-boundary",
            state: { settingsReturnTo: "/journal?day=2026-07-11" },
          },
        ]}
      >
        <TypographyScaleProvider initialScale="standard">
          <SettingsPage registryOverride={nativeSettingsRegistry} />
        </TypographyScaleProvider>
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", { name: "Journal settings" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        document.querySelector('[data-setting-id="wb.journal.day-boundary"]'),
      ).toHaveFocus(),
    );

    rendered.unmount();
    render(
      <MemoryRouter initialEntries={["/settings/setting/example.missing"]}>
        <TypographyScaleProvider initialScale="standard">
          <SettingsPage registryOverride={nativeSettingsRegistry} />
        </TypographyScaleProvider>
      </MemoryRouter>,
    );
    expect(
      screen.getByRole("heading", { name: "Setting not found" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/example\.missing/)).toBeInTheDocument();
  });

  it("preserves a dirty time draft when an external settings change reconciles", async () => {
    let valueRequest = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (!url.startsWith("/api/settings/values?")) {
        return new Response(null, { status: 404 });
      }
      valueRequest += 1;
      const authoritative = valueRequest === 1 ? "05:00" : "04:00";
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
            effective_value: authoritative,
            configured_value: authoritative,
            source: valueRequest === 1 ? "default" : "profile",
            is_modified: valueRequest > 1,
            revision: `value:${valueRequest}`,
          },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <DashboardEventProvider>
        <MemoryRouter initialEntries={["/settings/apps/journal"]}>
          <TypographyScaleProvider initialScale="standard">
            <SettingsPage registryOverride={nativeSettingsRegistry} />
          </TypographyScaleProvider>
        </MemoryRouter>
      </DashboardEventProvider>,
    );
    const input = await screen.findByLabelText("Day starts");
    await waitFor(() => expect(input).toBeEnabled());
    fireEvent.change(input, { target: { value: "03:30" } });
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));

    act(() =>
      MockEventSource.instances[0]!.message({
        event_type: "settings.changed",
        payload: { setting_ids: ["wb.journal.day-boundary"] },
        ts: 1_789_000_000,
      }),
    );

    await screen.findByText(/authoritative value changed to 4:00 AM/i);
    expect(input).toHaveValue("03:30");
    fireEvent.click(screen.getByRole("button", { name: "Use latest value" }));
    expect(input).toHaveValue("04:00");
  });

  it("surfaces authoritative timezone drift instead of silently using one zone", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (!String(input).startsWith("/api/settings/values?")) {
        return new Response(null, { status: 404 });
      }
      return Response.json({
        schema_version: 1,
        registry_revision: "settings-registry:1",
        timezone: "America/Toronto",
        configured_timezone: "America/New_York",
        observed_at: "2026-07-15T12:00:00Z",
        read_only: false,
        diagnostics: [{
          code: "timezone_config_drift",
          active_timezone: "America/Toronto",
          configured_timezone: "America/New_York",
          message: "The configured Work Buddy timezone differs from the Journal policy timezone.",
        }],
        values: [{
          setting_id: "wb.journal.day-boundary",
          scope: { kind: "profile", subject_id: "default" },
          effective_value: "05:00",
          configured_value: "05:00",
          source: "default",
          is_modified: false,
          revision: "value:0",
        }],
      });
    }));

    render(
      <MemoryRouter initialEntries={["/settings/apps/journal"]}>
        <TypographyScaleProvider initialScale="standard">
          <SettingsPage registryOverride={nativeSettingsRegistry} />
        </TypographyScaleProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/configured Work Buddy timezone differs/i)).toBeInTheDocument();
    expect(screen.getByText("timezone_config_drift")).toBeInTheDocument();
  });

  it("formats backend preview instants in the configured Work Buddy timezone", () => {
    const formatted = formatInstantInTimeZone(
      "2026-07-15T12:00:00Z",
      "Asia/Tokyo",
    );
    expect(formatted).toMatch(/9:00/);
    expect(formatted).not.toMatch(/8:00 AM/);
    expect(settingsFocusScrollBehavior(true)).toBe("auto");
    expect(settingsFocusScrollBehavior(false)).toBe("smooth");
  });
});
