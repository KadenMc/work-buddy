import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAccessibilityViolations } from "../../test/setup";
import { ThemeProvider } from "../../theme/ThemeProvider";
import WidgetLab from "./WidgetLab";
import {
  listReusableLabWidgets,
  WIDGET_LAB_HOST_STATES,
  WIDGET_LAB_SIZE_MODES,
} from "./labCases";

function renderLab(path = "/app/__widget-lab") {
  return render(
    <ThemeProvider initialPreference={{ scheme: "light", skinId: "wb.default" }}>
      <MemoryRouter initialEntries={[path]}>
        <WidgetLab />
      </MemoryRouter>
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

    expect(widgets).toHaveLength(3);
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
  });

  it("keeps a representative real-widget trace accessible", async () => {
    const { container } = renderLab("/app/__widget-lab?count=3");
    await screen.findByLabelText("Capture text");
    await waitFor(() =>
      expect(container.querySelector(".wb-day-timeline")).not.toBeNull(),
    );
    await waitFor(() =>
      expect(container.querySelector(".wb-running-notes")).not.toBeNull(),
    );

    await expectNoAccessibilityViolations(container);
  });
});
