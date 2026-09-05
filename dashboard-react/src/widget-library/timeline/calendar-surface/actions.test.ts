import { describe, expect, it } from "vitest";

import type { CalendarSurfaceItem } from "./contracts";
import {
  CALENDAR_RECORD_DELETE_ACTION_ID,
  CALENDAR_RECORD_EDIT_ACTION_ID,
  defaultCalendarItemActions,
} from "./actions";

const item = (
  kind: CalendarSurfaceItem["kind"],
  capabilities: CalendarSurfaceItem["capabilities"],
): CalendarSurfaceItem => ({
  id: `${kind}-item`,
  revision: "r1",
  sourceId: kind,
  placement: { shape: "point", at: "2026-07-11T12:00:00-04:00" },
  kind,
  title: `${kind} item`,
  status: kind === "record" ? "observed" : "planned",
  provenance: { source: kind, label: kind },
  capabilities,
  navigation: { targetType: "fixture", targetId: `${kind}:1` },
});

const editable = { open: true, move: true, resize: true, remove: true } as const;
const fixed = { open: true, move: false, resize: false, remove: false } as const;
const correctable = {
  open: true,
  move: false,
  resize: false,
  remove: true,
  edit: true,
} as const;

describe("defaultCalendarItemActions", () => {
  it("offers a correctable record its own text and time, never a reschedule", () => {
    const result = defaultCalendarItemActions(item("record", correctable), {
      mode: "read_write",
    });
    expect(result.actions.map((action) => action.label)).toEqual(["Edit", "Delete"]);
    expect(result.actions.map((action) => action.id)).toEqual([
      CALENDAR_RECORD_EDIT_ACTION_ID,
      CALENDAR_RECORD_DELETE_ACTION_ID,
    ]);
    expect(result.actions.every((action) => action.dispatch === "action")).toBe(true);
    expect(result.note).toBeUndefined();
  });

  it("offers a record authored elsewhere nothing it cannot carry out", () => {
    const result = defaultCalendarItemActions(item("record", fixed), {
      mode: "read_write",
    });
    expect(result.actions).toEqual([]);
    expect(result.note).toMatch(/authored elsewhere/);
  });

  it("keeps records retrospective even if a malformed projection grants mutations", () => {
    const result = defaultCalendarItemActions(item("record", editable), {
      mode: "read_write",
    });
    expect(result.actions.map((action) => action.label)).toEqual(["Delete"]);
  });

  it("withholds record corrections from a read-only day", () => {
    const result = defaultCalendarItemActions(item("record", correctable), {
      mode: "read_only",
      reason: "This Journal day is closed.",
    });
    expect(result.actions).toEqual([]);
    expect(result.note).toBe("This Journal day is closed.");
  });

  it("exposes plan scheduling actions from capabilities", () => {
    const result = defaultCalendarItemActions(item("plan", editable), {
      mode: "read_write",
    });
    expect(result.actions.map((action) => action.label)).toEqual([
      "Open plan",
      "Go to plan source",
      "Edit scheduled time",
      "Change duration",
      "Remove plan",
    ]);
  });

  it("keeps fixed calendar events provider-read-only", () => {
    const result = defaultCalendarItemActions(item("calendar", fixed), {
      mode: "read_write",
    });
    expect(result.actions.map((action) => action.label)).toEqual([
      "Open event",
      "View in source calendar",
    ]);
    expect(result.note).toMatch(/Provider editing is not connected/);
  });

  it("intersects item capabilities with view-level read-only access", () => {
    const result = defaultCalendarItemActions(item("plan", editable), {
      mode: "read_only",
      reason: "Fixture is locked",
    });
    expect(result.actions.map((action) => action.label)).toEqual([
      "Open plan",
      "Go to plan source",
    ]);
    expect(result.note).toBe("Fixture is locked");
  });

  it("provides safe generic defaults for namespaced App item kinds", () => {
    const result = defaultCalendarItemActions(
      item("app:acme.milestone", editable),
      { mode: "read_write" },
    );
    expect(result.actions.map((action) => action.label)).toEqual([
      "Open item",
      "Go to item source",
      "Edit scheduled time",
      "Change duration",
      "Remove item",
    ]);
  });
});
