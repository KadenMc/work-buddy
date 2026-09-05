import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  asViewId,
  asWidgetInstanceId,
  type WidgetPresentationContext,
} from "../../dashboard/contributions/contracts";
import { expectNoAccessibilityViolations } from "../../test/setup";
import { WidgetDraftTestScope } from "../../test/DashboardTestRuntime";
import { fallbackCanvasTheme } from "../../theme/resolveTheme";
import { TIMELINE_APP_CONTRIBUTION } from "./contribution";
import type { DayTimelineInput, DayTimelineItem } from "./contracts";
import DayTimelineWidget from "./DayTimelineWidget";

const presentation: WidgetPresentationContext = {
  instanceId: asWidgetInstanceId("instance-timeline-test"),
  viewId: asViewId("example.host.main"),
  width: 960,
  height: 720,
  sizeMode: "standard",
  interactionMode: "operate",
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

/** A record the Journal provider authors itself, so its text and time are its own. */
const correctableItem: DayTimelineItem = {
  ...pointItem,
  text: "Captured decision\nwith a second line",
  version: 4,
  authorityKind: "native_plain",
};

/** A record whose content belongs to another authority and cannot change here. */
const importedItem: DayTimelineItem = {
  ...pointItem,
  itemId: "record-imported",
  title: "Imported decision",
  text: "Imported decision",
  version: 4,
  authorityKind: "legacy_entry",
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

const renderTimeline = (
  widgetInput: DayTimelineInput,
  emit: ReturnType<typeof vi.fn>,
  hostPresentation: WidgetPresentationContext = presentation,
) => (
  <WidgetDraftTestScope
    definition={TIMELINE_APP_CONTRIBUTION.widgetDefinitions[0]}
    presentation={hostPresentation}
    input={widgetInput}
  >
    <DayTimelineWidget
      input={widgetInput}
      emit={emit as ComponentProps<typeof DayTimelineWidget>["emit"]}
      presentation={hostPresentation}
    />
  </WidgetDraftTestScope>
);

const openInspector = async (name: RegExp) => {
  await userEvent.click(await screen.findByRole("button", { name }));
  return screen.findByRole("dialog");
};

describe("DayTimelineWidget", () => {
  it("keeps the presentation control discoverable at compact size without changing the selected mode", async () => {
    const emit = vi.fn();
    const { container } = render(
      renderTimeline(input, emit, { ...presentation, sizeMode: "compact" }),
    );

    expect(screen.getByRole("radio", { name: "Timeline" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "List" })).toBeVisible();
    expect(
      screen.getByRole("region", { name: "Calendar surface for 2026-07-11" }),
    ).toHaveAttribute("data-wb-calendar-view", "calendar:day");
    expect(emit).not.toHaveBeenCalledWith(
      expect.objectContaining({ intent_type: "wb.timeline.render-mode-changed" }),
    );
    await expectNoAccessibilityViolations(container);
  });

  it("opens the same typed inspector and action path from the list presentation", async () => {
    const emit = vi.fn().mockResolvedValue({ status: "accepted", revision: "r1" });
    render(
      renderTimeline({ ...input, renderMode: "list" }, emit, {
        ...presentation,
        sizeMode: "compact",
      }),
    );

    expect(screen.getByRole("radio", { name: "List" })).toBeChecked();
    expect(
      screen.getByRole("region", { name: "Calendar surface for 2026-07-11" }),
    ).toHaveAttribute("data-wb-calendar-view", "list:day");

    const recordInspector = await openInspector(/Captured decision/);
    expect(recordInspector).toHaveTextContent("Captured decision");
    expect(recordInspector).toHaveTextContent("past — protected");
    await userEvent.keyboard("{Escape}");

    await openInspector(/Product stand-up/);
    await userEvent.click(screen.getByRole("button", { name: "Open event" }));
    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: "wb.timeline.open-item",
        view_id: presentation.viewId,
        instance_id: presentation.instanceId,
        payload: { item_id: "calendar-1" },
      }),
    );
  });

  it("emits display and replan intents without domain routing", async () => {
    const emit = vi.fn();
    render(
      renderTimeline(input, emit, { ...presentation, sizeMode: "expanded" }),
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

  it("promotes point records into the FullCalendar surface without span styling", () => {
    const nearbyPoint: DayTimelineItem = {
      ...pointItem,
      itemId: "record-nearby",
      at: "2026-07-11T09:30:00-04:00",
      title: "Nearby captured decision",
    };
    render(
      renderTimeline({ ...input, items: [pointItem, nearbyPoint] }, vi.fn()),
    );

    expect(
      screen.getByRole("region", { name: "Calendar surface for 2026-07-11" }),
    ).toHaveAttribute("data-wb-calendar-view", "calendar:day");
    const first = document.querySelector('[data-wb-calendar-item-id="record-1"]');
    const second = document.querySelector(
      '[data-wb-calendar-item-id="record-nearby"]',
    );
    expect(first).toHaveClass("wb-calendar-event--point");
    expect(second).toHaveClass("wb-calendar-event--point");
    expect(first).toHaveAttribute("aria-haspopup", "dialog");
  });

  it("keeps a heavy collection available in semantic document order", () => {
    const heavyItems = Array.from({ length: 180 }, (_, index): DayTimelineItem => ({
      ...pointItem,
      itemId: `record-${index}`,
      at: new Date(Date.parse(input.day.windowStart) + index * 60_000).toISOString(),
      title: `Heavy item ${index}`,
    }));
    const { container } = render(
      renderTimeline({ ...input, renderMode: "list", items: heavyItems }, vi.fn(), {
        ...presentation,
        sizeMode: "compact",
      }),
    );

    expect(screen.getAllByText("Heavy item 179").length).toBeGreaterThan(0);
    expect(container.querySelectorAll("[data-wb-calendar-item-id]")).toHaveLength(180);
  });

  it("defers a whole-view access notice while keeping timeline changes disabled", () => {
    render(
      renderTimeline(
        {
          ...input,
          access: { mode: "read_only", reason: "Editing is paused." },
          accessNotice: "view",
        },
        vi.fn(),
        { ...presentation, sizeMode: "expanded" },
      ),
    );

    expect(screen.queryByText("Editing is paused.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Request replan" })).toBeDisabled();
  });

  it("offers correction only on a record the provider authors", async () => {
    render(
      renderTimeline(
        { ...input, renderMode: "list", items: [correctableItem, importedItem] },
        vi.fn(),
      ),
    );

    const correctable = await openInspector(/Captured decision/);
    expect(correctable).toHaveTextContent("editable");
    expect(screen.getByRole("button", { name: "Edit" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Delete" })).toBeVisible();
    await userEvent.keyboard("{Escape}");

    const imported = await openInspector(/Imported decision/);
    expect(imported).toHaveTextContent(/authored elsewhere/);
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("withholds correction on a read-only day", async () => {
    render(
      renderTimeline(
        {
          ...input,
          renderMode: "list",
          items: [correctableItem],
          access: { mode: "read_only", reason: "This Journal day is closed." },
          accessNotice: "view",
        },
        vi.fn(),
      ),
    );

    const inspector = await openInspector(/Captured decision/);
    expect(inspector).toHaveTextContent("This Journal day is closed.");
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("carries the record's numeric version and resolved time through an edit", async () => {
    const emit = vi.fn().mockResolvedValue({ status: "accepted", revision: "r2" });
    const { container } = render(
      renderTimeline(
        { ...input, renderMode: "list", items: [correctableItem] },
        emit,
      ),
    );

    await openInspector(/Captured decision/);
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));

    const editor = await screen.findByRole("textbox", { name: "Record text" });
    expect(editor).toHaveValue("Captured decision\nwith a second line");
    const timeField = screen.getByLabelText("Record time");
    expect(timeField).toHaveValue("09:05");

    await userEvent.clear(editor);
    await userEvent.type(editor, "Corrected decision");
    await userEvent.clear(timeField);
    await userEvent.type(timeField, "10:45");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: "wb.timeline.item-edit-requested",
        client_mutation_id: expect.stringMatching(/^timeline-edit:/),
        view_id: presentation.viewId,
        instance_id: presentation.instanceId,
        payload: {
          item_id: "record-1",
          expected_version: 4,
          text: "Corrected decision",
          stated_at: "2026-07-11T10:45:00-04:00",
        },
      }),
    );
    expect(await screen.findAllByText("Corrected decision")).not.toHaveLength(0);
    expect(screen.queryAllByText("Captured decision")).toHaveLength(0);
    await expectNoAccessibilityViolations(container);
  });

  it("leaves the occurrence time unstated when only the text changed", async () => {
    const emit = vi.fn().mockResolvedValue({ status: "accepted", revision: "r2" });
    render(
      renderTimeline(
        { ...input, renderMode: "list", items: [correctableItem] },
        emit,
      ),
    );

    await openInspector(/Captured decision/);
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const editor = await screen.findByRole("textbox", { name: "Record text" });
    await userEvent.clear(editor);
    await userEvent.type(editor, "Corrected decision");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(emit.mock.calls[0]?.[0].payload).toEqual({
      item_id: "record-1",
      expected_version: 4,
      text: "Corrected decision",
    });
  });

  it("shows a conflicting edit as a visible failure rather than a silent no-op", async () => {
    const emit = vi.fn().mockResolvedValue({
      status: "conflict",
      revision: "r2",
      message: "The Journal item changed before this edit.",
    });
    render(
      renderTimeline(
        { ...input, renderMode: "list", items: [correctableItem] },
        emit,
      ),
    );

    await openInspector(/Captured decision/);
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const editor = await screen.findByRole("textbox", { name: "Record text" });
    await userEvent.clear(editor);
    await userEvent.type(editor, "Corrected decision");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("The Journal item changed before this edit."),
    ).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Record text" })).toHaveValue(
      "Corrected decision",
    );
  });

  it("confirms a deletion and emits a versioned, idempotent mutation", async () => {
    const emit = vi.fn().mockResolvedValue({ status: "accepted", revision: "r2" });
    render(
      renderTimeline(
        { ...input, renderMode: "list", items: [correctableItem] },
        emit,
      ),
    );

    await openInspector(/Captured decision/);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(
      screen.getByRole("alertdialog", { name: "Delete this record?" }),
    ).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Keep record" }));
    expect(emit).not.toHaveBeenCalled();

    // The confirmation takes focus, so the inspector closes behind it.
    await openInspector(/Captured decision/);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete record" }));

    expect(emit).toHaveBeenCalledWith(
      expect.objectContaining({
        intent_type: "wb.timeline.item-delete-requested",
        client_mutation_id: expect.stringMatching(/^timeline-delete:/),
        payload: { item_id: "record-1", expected_version: 4 },
      }),
    );
    expect(
      await screen.findByText("No temporal items for this day."),
    ).toBeVisible();
  });

  it("shows a refused deletion as a visible failure", async () => {
    const emit = vi.fn().mockResolvedValue({
      status: "conflict",
      revision: "r2",
      message: "The Journal item changed before this action.",
    });
    render(
      renderTimeline(
        { ...input, renderMode: "list", items: [correctableItem] },
        emit,
      ),
    );

    await openInspector(/Captured decision/);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete record" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The Journal item changed before this action.");
    expect(screen.getAllByText(/Captured decision/).length).toBeGreaterThan(0);
  });
});
