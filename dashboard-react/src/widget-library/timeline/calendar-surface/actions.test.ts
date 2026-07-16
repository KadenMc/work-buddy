import { describe, expect, it } from "vitest";

import type { CalendarSurfaceItem } from "./contracts";
import { defaultCalendarItemActions } from "./actions";

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

describe("defaultCalendarItemActions", () => {
  it("keeps records retrospective even if a malformed projection grants mutations", () => {
    const result = defaultCalendarItemActions(item("record", editable), {
      mode: "read_write",
    });
    expect(result.actions.map((action) => action.label)).toEqual([
      "Open record",
      "Go to record source",
    ]);
    expect(result.note).toMatch(/observed work/);
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
