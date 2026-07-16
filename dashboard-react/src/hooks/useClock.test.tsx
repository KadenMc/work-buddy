import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { DashboardTemporalContext } from "../dashboard/temporal/DashboardTemporalContext";
import { formatClock, useClock } from "./useClock";

const INSTANT = new Date("2026-07-11T16:18:00.000Z");

function Probe({ context }: { readonly context?: DashboardTemporalContext }) {
  const time = useClock(context);
  return <output>{time ?? "unavailable"}</output>;
}

describe("dashboard clock", () => {
  it("formats one instant in the explicit Work Buddy zone", () => {
    expect(formatClock(INSTANT, "America/Toronto")).toBe("12:18 PM");
    expect(formatClock(INSTANT, "Pacific/Kiritimati")).toBe("06:18 AM");
  });

  it("does not fall back to browser-local formatting without context", () => {
    render(<Probe />);
    expect(screen.getByText("unavailable")).toBeInTheDocument();
  });

  it("starts from the server-observed instant", () => {
    render(
      <Probe
        context={{
          schemaVersion: 1,
          revision: "timezone:America/Toronto",
          timezone: "America/Toronto",
          now: INSTANT.toISOString(),
        }}
      />,
    );
    expect(screen.getByText("12:18 PM")).toBeInTheDocument();
  });
});
