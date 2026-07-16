import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  DASHBOARD_CONTEXT_ENDPOINT,
  DashboardTemporalContextProvider,
  parseDashboardTemporalContext,
  useDashboardTemporalContext,
  type DashboardTemporalContext,
} from "./DashboardTemporalContext";

const CONTEXT: DashboardTemporalContext = {
  schemaVersion: 1,
  revision: "timezone:America/Toronto",
  timezone: "America/Toronto",
  now: "2026-07-11T16:18:00.000Z",
};

function Probe() {
  const state = useDashboardTemporalContext();
  return (
    <output data-testid="temporal">
      {state.status === "ready"
        ? `${state.context.timezone}|${state.context.now}`
        : state.status}
    </output>
  );
}

describe("DashboardTemporalContextProvider", () => {
  it("loads and validates the host-owned temporal context", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          schema_version: 1,
          revision: CONTEXT.revision,
          timezone: CONTEXT.timezone,
          now: CONTEXT.now,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(
      <DashboardTemporalContextProvider fetchImpl={fetchImpl}>
        <Probe />
      </DashboardTemporalContextProvider>,
    );

    expect(screen.getByTestId("temporal")).toHaveTextContent("loading");
    await waitFor(() =>
      expect(screen.getByTestId("temporal")).toHaveTextContent(
        "America/Toronto|2026-07-11T16:18:00.000Z",
      ),
    );
    expect(fetchImpl).toHaveBeenCalledWith(DASHBOARD_CONTEXT_ENDPOINT, {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      signal: expect.any(AbortSignal),
    });
  });

  it("never invents a browser timezone when the host contract is invalid", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          schema_version: 1,
          revision: "bad",
          timezone: "Not/AZone",
          now: CONTEXT.now,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(
      <DashboardTemporalContextProvider fetchImpl={fetchImpl}>
        <Probe />
      </DashboardTemporalContextProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("temporal")).toHaveTextContent("unavailable"),
    );
  });
});

describe("parseDashboardTemporalContext", () => {
  it("maps the wire contract without changing its timezone", () => {
    expect(
      parseDashboardTemporalContext({
        schema_version: 1,
        revision: CONTEXT.revision,
        timezone: CONTEXT.timezone,
        now: CONTEXT.now,
      }),
    ).toEqual(CONTEXT);
  });
});
