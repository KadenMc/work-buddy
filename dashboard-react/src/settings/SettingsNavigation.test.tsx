import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import {
  resolveSettingsReturnPath,
  SettingsLauncher,
} from "./SettingsNavigation";

function LocationProbe() {
  const location = useLocation();
  return <output>{`${location.pathname}${location.search}`}</output>;
}

describe("SettingsNavigation", () => {
  it("opens settings as dashboard chrome and remembers the current view", () => {
    render(
      <MemoryRouter initialEntries={["/journal?day=2026-07-11"]}>
        <SettingsLauncher defaultViewPath="/journal" />
        <LocationProbe />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Open settings" }));

    expect(
      screen.getByText("/settings/system/accessibility"),
    ).toBeInTheDocument();
  });

  it("returns to the remembered view and rejects unsafe or recursive targets", () => {
    expect(
      resolveSettingsReturnPath(
        { settingsReturnTo: "/journal?day=2026-07-11" },
        "/journal",
      ),
    ).toBe("/journal?day=2026-07-11");
    expect(
      resolveSettingsReturnPath(
        { settingsReturnTo: "/settings/system/accessibility" },
        "/journal",
      ),
    ).toBe("/journal");
    expect(
      resolveSettingsReturnPath(
        { settingsReturnTo: "//outside.example" },
        "/journal",
      ),
    ).toBe("/journal");
  });

  it("uses the active dashboard-owned gear to return to the remembered view", () => {
    render(
      <MemoryRouter
        initialEntries={[
          {
            pathname: "/settings/system/accessibility",
            state: { settingsReturnTo: "/journal?day=2026-07-11" },
          },
        ]}
      >
        <SettingsLauncher defaultViewPath="/journal" />
        <LocationProbe />
      </MemoryRouter>,
    );

    const closeSettings = screen.getByRole("button", { name: "Close settings" });
    expect(closeSettings).toBeEnabled();
    expect(closeSettings).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(closeSettings);
    expect(screen.getByText("/journal?day=2026-07-11")).toBeInTheDocument();
  });
});
