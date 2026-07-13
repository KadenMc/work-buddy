import { describe, expect, it } from "vitest";

import type { DayTimelineInput, DayTimelineItem } from "../contracts";
import { toCalendarEngineEventInputs } from "./fullcalendar/toFullCalendarEventInputs";
import { toCalendarSurfaceModel } from "./fromDayTimeline";

const point: DayTimelineItem = {
  itemId: "record:captured",
  kind: "record",
  shape: "point",
  at: "2026-07-11T12:18:00-04:00",
  title: "Captured decision",
  status: "observed",
  mutability: "past_protected",
  precision: "exact",
  provenance: { source: "user", label: "you" },
};

const input: DayTimelineInput = {
  instanceId: "default:timeline",
  revision: "journal:r9",
  day: {
    dayId: "journal-day:2026-07-11",
    localDate: "2026-07-11",
    timezone: "America/New_York",
    dayBoundaryStart: "05:00",
    windowStart: "2026-07-11T05:00:00-04:00",
    windowEnd: "2026-07-12T05:00:00-04:00",
    now: "2026-07-11T12:18:00-04:00",
  },
  access: { mode: "read_write" },
  renderMode: "timeline",
  density: "comfortable",
  items: [point],
};

describe("toCalendarSurfaceModel", () => {
  it("preserves a Journal record as a point without inventing an end", () => {
    const model = toCalendarSurfaceModel(input);
    expect(model.items[0]?.placement).toEqual({
      shape: "point",
      at: point.at,
    });
    expect(model.capabilities).toEqual({ create: false });

    const engineEvent = toCalendarEngineEventInputs(model)[0];
    expect(engineEvent).toMatchObject({
      id: point.itemId,
      start: point.at,
      allDay: false,
      extendedProps: { point: true },
    });
    expect(engineEvent).not.toHaveProperty("end");
  });

  it("keeps Journal range and presentation independent", () => {
    const model = toCalendarSurfaceModel({ ...input, renderMode: "list" });
    expect(model.view).toEqual({ range: "day", presentation: "list" });
    expect(model.visibleRange).toEqual({
      start: input.day.windowStart,
      endExclusive: input.day.windowEnd,
    });
  });
});
