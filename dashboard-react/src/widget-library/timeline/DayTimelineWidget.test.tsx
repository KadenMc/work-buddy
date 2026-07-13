import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../dashboard/contributions/contracts";
import { expectNoAccessibilityViolations } from "../../test/setup";
import { fallbackCanvasTheme } from "../../theme/resolveTheme";
import type { DayTimelineInput, DayTimelineItem } from "./contracts";
import DayTimelineWidget from "./DayTimelineWidget";

const presentation: WidgetPresentationContext = {
  instanceId: asWidgetInstanceId("instance-timeline-test"),
  viewId: asViewId("example.host.main"),
  width: 960,
  height: 720,
  sizeMode: "standard",
  editing: false,
  theme: {
    contractVersion: 1,
    preference: { scheme: "dark", skinId: "wb.conformance-stress" },
    resolvedScheme: "dark",
    skin: {
      id: "wb.conformance-stress",
      version: 1,
      publisherAppId: "wb.core",
    },
    accessibility: {
      forcedColors: false,
      reducedMotion: true,
      reducedTransparency: false,
    },
  },
  getCanvasTheme: () => fallbackCanvasTheme("dark"),
};

const pointItem: DayTimelineItem = {
  itemId: "record-1",
  kind: "record",
  shape: "point",
  at: "2026-07-11T09:05:00-04:00",
  title: "Captured decision",
  status: "observed",
  mutability: "past_protected",
  precision: "exact",
  provenance: { source: "user", label: "you" },
};

const spanItem: DayTimelineItem = {
  itemId: "calendar-1",
  kind: "calendar",
  shape: "span",
  startAt: "2026-07-11T10:30:00-04:00",
  endAt: "2026-07-11T11:15:00-04:00",
  title: "Product stand-up",
  detail: "calendar · 45m",
  status: "planned",
  mutability: "fixed",
  precision: "exact",
  provenance: { source: "calendar", label: "calendar" },
};

const input: DayTimelineInput = {
  instanceId: "instance-timeline-test",
  revision: "r1",
  day: {
    dayId: "day-1",
    localDate: "2026-07-11",
    timezone: "America/New_York",
    dayBoundaryStart: "05:00",
    windowStart: "2026-07-11T05:00:00-04:00",
    windowEnd: "2026-07-12T05:00:00-04:00",
    now: "2026-07-11T12:18:00-04:00",
  },
  renderMode: "timeline",
  density: "comfortable",
  items: [pointItem, spanItem],
};

describe("DayTimelineWidget", () => {
  it("uses an accessible compact list and emits a generic open intent", async () => {
    const emit = vi.fn();
    const { container } = render(
      <DayTimelineWidget
        input={input}
        emit={emit}
        presentation={{ ...presentation, sizeMode: "compact" }}
      />,
    );

    const item = screen.getByRole("button", { name: /Captured decision/ });
    await userEvent.click(item);
    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: "wb.timeline.open-item",
        view_id: presentation.viewId,
        instance_id: presentation.instanceId,
        payload: { item_id: "record-1" },
      }),
    );
    expect(screen.getByText("past — protected")).toBeInTheDocument();
    expect(screen.getByText("fixed commitment")).toBeInTheDocument();
    await expectNoAccessibilityViolations(container);
  });

  it("emits display and replan intents without domain routing", async () => {
    const emit = vi.fn();
    render(
      <DayTimelineWidget
        input={input}
        emit={emit}
        presentation={{ ...presentation, sizeMode: "expanded" }}
      />,
    );

    await userEvent.click(screen.getByRole("radio", { name: "List" }));
    await userEvent.click(screen.getByRole("button", { name: "Request replan" }));

    expect(emit.mock.calls.map(([intent]) => intent.intent_type)).toEqual([
      "wb.timeline.render-mode-changed",
      "wb.timeline.replan-requested",
    ]);
    expect(emit.mock.calls[1]?.[0].payload).toEqual({
      day_id: "day-1",
      preserve_before: "2026-07-11T12:18:00-04:00",
    });
  });

  it("allocates nearby short items to separate visual lanes", () => {
    const nearbyPoint: DayTimelineItem = {
      ...pointItem,
      itemId: "record-nearby",
      at: "2026-07-11T09:30:00-04:00",
      title: "Nearby captured decision",
    };
    render(
      <DayTimelineWidget
        input={{ ...input, items: [pointItem, nearbyPoint] }}
        emit={vi.fn()}
        presentation={presentation}
      />,
    );

    const first = screen.getByText("Captured decision").closest("button");
    const second = screen.getByText("Nearby captured decision").closest("button");
    expect(first).not.toBeNull();
    expect(second).not.toBeNull();
    if (first === null || second === null) throw new Error("Timeline buttons were not rendered");
    expect(first.style.getPropertyValue("--wb-timeline-left")).toBe("0%");
    expect(second.style.getPropertyValue("--wb-timeline-left")).toBe("50%");
  });

  it("keeps a heavy collection available in semantic document order", () => {
    const heavyItems = Array.from({ length: 180 }, (_, index): DayTimelineItem => ({
      ...pointItem,
      itemId: `record-${index}`,
      at: new Date(Date.parse(input.day.windowStart) + index * 60_000).toISOString(),
      title: `Heavy item ${index}`,
    }));
    const { container } = render(
      <DayTimelineWidget
        input={{ ...input, renderMode: "list", items: heavyItems }}
        emit={vi.fn()}
        presentation={{ ...presentation, sizeMode: "compact" }}
      />,
    );

    expect(screen.getByText("Heavy item 179")).toBeInTheDocument();
    expect(container.querySelectorAll(".wb-temporal-list > li")).toHaveLength(180);
  });
});
