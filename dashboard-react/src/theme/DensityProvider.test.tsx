import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import {
  DASHBOARD_DENSITY_STORAGE_KEY,
  DensityProvider,
  readDashboardDensity,
  useDensity,
  writeDashboardDensity,
} from "./DensityProvider";

function DensityHarness() {
  const { density, setDensity } = useDensity();
  return (
    <button type="button" onClick={() => setDensity("compact")}>
      {density}
    </button>
  );
}

afterEach(() => {
  localStorage.removeItem(DASHBOARD_DENSITY_STORAGE_KEY);
  delete document.documentElement.dataset.wbDensity;
});

describe("dashboard density", () => {
  it("falls back safely and round-trips only known density values", () => {
    localStorage.setItem(DASHBOARD_DENSITY_STORAGE_KEY, "unknown");
    expect(readDashboardDensity()).toBe("comfortable");

    writeDashboardDensity("spacious");
    expect(readDashboardDensity()).toBe("spacious");
  });

  it("publishes and persists the active density through the shared provider", async () => {
    const user = userEvent.setup();
    render(
      <DensityProvider initialDensity="spacious">
        <DensityHarness />
      </DensityProvider>,
    );

    expect(document.documentElement).toHaveAttribute("data-wb-density", "spacious");
    await user.click(screen.getByRole("button", { name: "spacious" }));
    expect(document.documentElement).toHaveAttribute("data-wb-density", "compact");
    expect(localStorage.getItem(DASHBOARD_DENSITY_STORAGE_KEY)).toBe("compact");
  });
});
