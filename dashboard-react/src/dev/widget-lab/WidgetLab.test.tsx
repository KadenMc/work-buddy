import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import { DashboardTestRuntime } from "../../test/DashboardTestRuntime";
import { ThemeProvider } from "../../theme/ThemeProvider";
import {
  JOURNAL_GENERIC_WIDGET_TYPE_ID,
  JOURNAL_WIDGET_TYPE_IDS,
} from "../../apps/journal/bindings";
import { TASKS_WIDGET_TYPE_IDS } from "../../apps/tasks/bindings";
import { JOBS_WIDGET_ID } from "../../apps/jobs/contribution";
import WidgetLab from "./WidgetLab";
import {
  buildStateCases,
  listReusableLabWidgets,
  WIDGET_LAB_HOST_STATES,
  WIDGET_LAB_SIZE_MODES,
} from "./labCases";

function renderLab(path = "/app/__widget-lab") {
  return render(
    <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
      <DashboardTestRuntime>
        <MemoryRouter initialEntries={[path]}>
          <WidgetLab />
        </MemoryRouter>
      </DashboardTestRuntime>
    </ThemeProvider>,
  );
}

describe("WidgetLab", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        media: query,
        matches: false,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
  });

  it("mounts every registered standard widget across every size mode and host state", async () => {
    renderLab();
    const widgets = listReusableLabWidgets();
    const cases = await screen.findAllByTestId("widget-lab-host");

    expect(widgets.map((widget) => widget.definition.typeId).sort()).toEqual([
      JOURNAL_WIDGET_TYPE_IDS.capture,
      JOURNAL_WIDGET_TYPE_IDS.timeline,
      JOURNAL_WIDGET_TYPE_IDS.runningNotes,
      JOURNAL_GENERIC_WIDGET_TYPE_ID,
      TASKS_WIDGET_TYPE_IDS.quickAdd,
      TASKS_WIDGET_TYPE_IDS.workspace,
      JOBS_WIDGET_ID,
    ].sort());
    expect(cases).toHaveLength(
      widgets.length * (WIDGET_LAB_SIZE_MODES.length + WIDGET_LAB_HOST_STATES.length),
    );
    for (const widget of widgets) {
      for (const sizeMode of WIDGET_LAB_SIZE_MODES) {
        expect(
          cases.some(
            (element) =>
              element.dataset.widgetType === widget.definition.typeId &&
              element.dataset.sizeMode === sizeMode &&
              element.dataset.hostState === "ready",
          ),
        ).toBe(true);
      }
      for (const status of WIDGET_LAB_HOST_STATES) {
        expect(
          cases.some(
            (element) =>
              element.dataset.widgetType === widget.definition.typeId &&
              element.dataset.hostState === status,
          ),
        ).toBe(true);
      }
    }
  });

  it("supplies deterministic Jobs registry and read-only inputs without opening assistance", () => {
    const jobs = buildStateCases().filter((item) => item.widget.definition.typeId === JOBS_WIDGET_ID);
    expect(jobs).toHaveLength(WIDGET_LAB_HOST_STATES.length);
    for (const item of jobs) {
      expect(item.input).toMatchObject({
        access: { mode: item.status === "read-only" ? "read_only" : "read_write" },
        timeZone: "America/New_York",
        capabilities: [{ name: "journal_state" }],
        workflows: [{ name: "morning-routine" }],
        openAssistance: false,
      });
    }
  });

  it("mounts exactly the requested number of real WidgetHost frames for a trace", async () => {
    const { container } = renderLab("/app/__widget-lab?count=50");

    expect(await screen.findAllByTestId("widget-lab-host")).toHaveLength(50);
    await waitFor(() =>
      expect(container.querySelectorAll(".wb-widget-frame")).toHaveLength(50),
    );
    expect(
      screen.getByText("Synthetic trace: exactly 50 real widget hosts"),
    ).toBeInTheDocument();
  });

  it("switches scheme and skin through the shared ThemeProvider contract", async () => {
    renderLab("/app/__widget-lab?count=3");

    await userEvent.click(screen.getByRole("button", { name: /Widget Lab scheme/ }));
    await userEvent.click(await screen.findByRole("option", { name: "Dark" }));
    expect(document.documentElement).toHaveAttribute("data-wb-scheme", "dark");

    await userEvent.click(screen.getByRole("button", { name: /Widget Lab skin/ }));
    await userEvent.click(await screen.findByRole("option", { name: /Conformance stress/i }));
    expect(document.documentElement).toHaveAttribute(
      "data-wb-skin",
      "wb.conformance-stress",
    );
  }, 60_000);

  it("keeps a representative real-widget trace accessible", async () => {
    const { container } = renderLab("/app/__widget-lab?count=3");
    // Real hosts resolve lazy renderers and restore drafts asynchronously. Use
    // the conformance readiness budget before running the unchanged axe checks.
    await waitFor(() => {
      expect(screen.getByLabelText("Capture text")).toBeInTheDocument();
      expect(container.querySelector(".wb-day-timeline")).not.toBeNull();
      expect(container.querySelector(".wb-running-notes")).not.toBeNull();
    }, { timeout: 10_000 });

    await expectNoAccessibilityViolations(container);
  });
});
