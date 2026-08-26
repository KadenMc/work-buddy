import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ViewSnapshot } from "../../dashboard/contributions/contracts";
import type { JobAuthoringInput } from "./contracts";
import { JOBS_VIEW_ID } from "./contribution";
import { createRuntime } from "./viewRuntime";

afterEach(() => vi.unstubAllGlobals());

function renderChrome(access: JobAuthoringInput["access"]) {
  const fetchImpl = vi.fn();
  vi.stubGlobal("fetch", fetchImpl);
  const runtime = createRuntime({
    search: "", storage: localStorage,
    location: { getSearch: () => "", pushSearch: vi.fn(), replaceSearch: vi.fn(), subscribe: () => () => {} },
  });
  const snapshot: ViewSnapshot = {
    viewId: JOBS_VIEW_ID, observedAt: "2026-08-26T12:00:00Z", status: access.mode === "read_only" ? "read-only" : "ready",
    quality: { kind: "complete" }, model: { access, timeZone: "America/New_York", capabilities: [], workflows: [] },
    bindings: {}, widgetInputs: {},
  };
  render(runtime.renderChrome(snapshot));
  return fetchImpl;
}

describe("Jobs view chrome", () => {
  it("keeps navigation visible without repeating the form's assistance introduction", () => {
    const fetchImpl = renderChrome({ mode: "read_write" });
    expect(screen.getByRole("heading", { name: "Jobs", level: 1 })).toBeVisible();
    expect(screen.getByRole("link", { name: "Manage existing jobs" })).toHaveAttribute("href", "/#tab=jobs");
    expect(screen.queryByText("Create a scheduled job, with as much help as you need.")).not.toBeInTheDocument();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("retains the actual read-only reason without requiring hover help", () => {
    const fetchImpl = renderChrome({ mode: "read_only", reason: "Open from the enrolled dashboard session." });
    expect(screen.getByText("Open from the enrolled dashboard session.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Manage existing jobs" })).toBeVisible();
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
