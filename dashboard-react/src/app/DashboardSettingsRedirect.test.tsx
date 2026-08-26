import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { LegacyDashboardSettingsRedirect } from "./DashboardApp";

function Destination() {
  const location = useLocation();
  return <output>{JSON.stringify({ path: `${location.pathname}${location.search}${location.hash}`, state: location.state })}</output>;
}

describe("legacy Dashboard settings redirect", () => {
  it.each([
    ["?setting=wb.dashboard.assistance&scope=profile", "?setting=wb.dashboard.assistance&scope=profile"],
    ["?setting=wb.dashboard.assistance-tier&scope=profile", "?setting=wb.dashboard.chat-execution-default&scope=profile"],
    ["?focus=wb.dashboard.assistance-tier", "?focus=wb.dashboard.chat-execution-default"],
  ])("preserves navigation state and hash while migrating %s", async (search, expected) => {
    const state = { settingsReturnTo: "/journal?day=2026-08-26" };
    render(<MemoryRouter initialEntries={[{ pathname: "/settings/apps/dashboard", search, hash: "#details", state }]}>
      <Routes><Route path="/settings/apps/dashboard" element={<LegacyDashboardSettingsRedirect />} />
        <Route path="/settings/system/dashboard-ai" element={<Destination />} /></Routes>
    </MemoryRouter>);
    expect(JSON.parse((await screen.findByRole("status")).textContent!)).toEqual({ path: `/settings/system/dashboard-ai${expected}#details`, state });
  });
});
