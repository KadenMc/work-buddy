import { describe, expect, it } from "vitest";

import {
  CALENDAR_SPIKE_CROSS_MIDNIGHT,
  CALENDAR_SPIKE_DENSE_200,
  CALENDAR_SPIKE_DST_SPRING,
  CALENDAR_SPIKE_JULY11,
  CALENDAR_SPIKE_MIXED_SOURCE,
  CALENDAR_SPIKE_READ_ONLY,
} from "../../../../dev/calendar-spike/fixtures";
import {
  calendarRangeRequestBounds,
  calendarWindowOptions,
} from "./FullCalendarSurfaceAdapter";
import { toCalendarEngineEventInputs } from "./toFullCalendarEventInputs";

describe("FullCalendar surface boundary", () => {
  it("keeps point semantics while allowing a private render-only duration", () => {
    const events = toCalendarEngineEventInputs(CALENDAR_SPIKE_JULY11);
    const point = events.find((event) => event.id === "mobile-edge-capture");

    expect(point).toMatchObject({
      start: "2026-07-11T11:51:00-04:00",
      allDay: false,
      durationEditable: false,
      extendedProps: { point: true },
    });
    expect(point).not.toHaveProperty("end");
    expect(
      CALENDAR_SPIKE_JULY11.items.find((item) => item.id === "mobile-edge-capture")
        ?.placement,
    ).toEqual({ shape: "point", at: "2026-07-11T11:51:00-04:00" });
  });

  it("intersects item capabilities with view-level read-only access", () => {
    const [event] = toCalendarEngineEventInputs(CALENDAR_SPIKE_READ_ONLY);

    expect(event?.id).toBe("read-only-editable-plan");
    expect(event?.editable).toBe(false);
    expect(event?.startEditable).toBe(false);
    expect(event?.durationEditable).toBe(false);
  });

  it("preserves exclusive all-day ends and maps only semantic tone classes", () => {
    const allDay = toCalendarEngineEventInputs(CALENDAR_SPIKE_MIXED_SOURCE).find(
      (event) => event.id === "mixed-all-day-calendar",
    );

    expect(allDay).toMatchObject({
      start: "2026-07-11",
      end: "2026-07-12",
      allDay: true,
    });
    expect(allDay?.classNames).toContain("wb-calendar-event--tone-data-4");
    expect(allDay?.classNames.join(" ")).not.toMatch(/#|rgb|hsl|style=/i);
  });

  it("maps every dense fixture item exactly once with stable identity", () => {
    const events = toCalendarEngineEventInputs(CALENDAR_SPIKE_DENSE_200);

    expect(events).toHaveLength(200);
    expect(new Set(events.map((event) => event.id)).size).toBe(200);
  });

  it("maps the 5 AM Journal window and initial scroll into engine durations", () => {
    expect(calendarWindowOptions(CALENDAR_SPIKE_JULY11)).toEqual({
      slotMinTime: "05:00:00",
      slotMaxTime: "29:00:00",
      scrollTime: "08:05:00",
    });
  });

  it("retains next-day windows and DST wall-clock bounds for the named-zone engine", () => {
    expect(calendarWindowOptions(CALENDAR_SPIKE_CROSS_MIDNIGHT)).toEqual({
      slotMinTime: "17:00:00",
      slotMaxTime: "32:00:00",
      scrollTime: "22:30:00",
    });
    expect(calendarWindowOptions(CALENDAR_SPIKE_DST_SPRING)).toEqual({
      slotMinTime: "00:00:00",
      slotMaxTime: "24:00:00",
      scrollTime: "00:10:00",
    });
  });

  it("keeps the exact logical-day range at the Work Buddy boundary when List spans civil dates", () => {
    const logicalDayList = {
      ...CALENDAR_SPIKE_JULY11,
      view: { range: "day", presentation: "list" },
    } as const;

    expect(
      calendarRangeRequestBounds(logicalDayList, {
        startStr: "2026-07-11T00:00:00-04:00",
        endStr: "2026-07-13T00:00:00-04:00",
      }),
    ).toEqual({
      start: "2026-07-11T05:00:00-04:00",
      endExclusive: "2026-07-12T05:00:00-04:00",
    });
  });
});
