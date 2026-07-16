import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { asSettingId } from "./contracts";
import { useSettingPreview } from "./useSettingPreview";

function payload(value: string) {
  return Response.json({
    schema_version: 1,
    registry_revision: "settings-registry:1",
    timezone: "America/Toronto",
    configured_timezone: "America/Toronto",
    value_revision: "value:0",
    preview: {
      setting_id: "wb.journal.day-boundary",
      scope: { kind: "profile", subject_id: "default" },
      value,
      effective_at: "2026-07-16T05:00:00-04:00",
      apply_status: "pending",
      impact_preview: {},
    },
    diagnostics: [],
  });
}

function Probe({ value, debounceMs = 0 }: { readonly value: string; readonly debounceMs?: number }) {
  const state = useSettingPreview({
    settingId: asSettingId("wb.journal.day-boundary"),
    value,
    expectedRevision: "value:0",
    enabled: true,
    debounceMs,
  });
  return <output>{`${state.status}:${String(state.preview?.value ?? "none")}`}</output>;
}

afterEach(() => vi.unstubAllGlobals());

describe("useSettingPreview", () => {
  it("debounces draft churn and requests only the latest valid value", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const value = JSON.parse(String(init?.body)).value as string;
      return payload(value);
    });
    vi.stubGlobal("fetch", fetchMock);

    const rendered = render(<Probe value="04:00" debounceMs={40} />);
    rendered.rerender(<Probe value="03:30" debounceMs={40} />);

    await screen.findByText("ready:03:30");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("ignores an aborted older response that settles after the latest preview", async () => {
    const resolvers = new Map<string, (response: Response) => void>();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        const value = JSON.parse(String(init?.body)).value as string;
        return new Promise<Response>((resolve) => resolvers.set(value, resolve));
      }),
    );

    const rendered = render(<Probe value="04:00" />);
    await waitFor(() => expect(resolvers.has("04:00")).toBe(true));
    rendered.rerender(<Probe value="03:30" />);
    await waitFor(() => expect(resolvers.has("03:30")).toBe(true));

    resolvers.get("03:30")!(payload("03:30"));
    await screen.findByText("ready:03:30");
    resolvers.get("04:00")!(payload("04:00"));

    await waitFor(() => expect(screen.getByText("ready:03:30")).toBeInTheDocument());
    expect(screen.queryByText("ready:04:00")).not.toBeInTheDocument();
  });
});
