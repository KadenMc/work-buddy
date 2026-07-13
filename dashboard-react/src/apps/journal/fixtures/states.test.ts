import { describe, expect, it } from "vitest";
import { JOURNAL_WIDGET_INSTANCE_IDS } from "../contracts";
import {
  JOURNAL_EMPTY_FIXTURE,
  JOURNAL_ERROR_FIXTURE,
  JOURNAL_HEAVY_DAY_FIXTURE,
  JOURNAL_LOADING_FIXTURE,
  JOURNAL_OFFLINE_FIXTURE,
  JOURNAL_PRE_0500_BOUNDARY_FIXTURE,
  JOURNAL_READ_ONLY_FIXTURE,
  JOURNAL_STALE_FIXTURE,
  JOURNAL_STATE_FIXTURES,
  PRE_0500_NOW,
} from "./states";

describe("Journal state fixtures", () => {
  it("represents loading and fatal error without inventing a model", () => {
    expect(JOURNAL_LOADING_FIXTURE.loadStatus).toBe("loading");
    expect(JOURNAL_LOADING_FIXTURE.model).toBeNull();
    expect(JOURNAL_ERROR_FIXTURE.loadStatus).toBe("error");
    expect(JOURNAL_ERROR_FIXTURE.model).toBeNull();
    expect(JOURNAL_ERROR_FIXTURE.error.retryable).toBe(true);
  });

  it("keeps empty content separate from load status", () => {
    expect(JOURNAL_EMPTY_FIXTURE.loadStatus).toBe("ready");
    expect(JOURNAL_EMPTY_FIXTURE.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline].items).toEqual(
      [],
    );
    expect(
      JOURNAL_EMPTY_FIXTURE.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].items,
    ).toEqual([]);
    expect(
      JOURNAL_EMPTY_FIXTURE.model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture]
        .capturesToday,
    ).toBe(0);
  });

  it("keeps stale and offline truthfulness distinct", () => {
    expect(JOURNAL_STALE_FIXTURE.loadStatus).toBe("stale");
    expect(JOURNAL_STALE_FIXTURE.model.quality.freshness).toBe("stale");
    expect(JOURNAL_STALE_FIXTURE.model.access.mode).toBe("read_write");

    expect(JOURNAL_OFFLINE_FIXTURE.loadStatus).toBe("offline");
    expect(JOURNAL_OFFLINE_FIXTURE.model.quality.freshness).toBe("offline");
    expect(JOURNAL_OFFLINE_FIXTURE.model.access.mode).toBe("read_only");
    expect(
      JOURNAL_OFFLINE_FIXTURE.model.widgetInputs[
        JOURNAL_WIDGET_INSTANCE_IDS.capture
      ].targets.every((target) => !target.enabled),
    ).toBe(true);
  });

  it("propagates read-only access into every mutating Journal input", () => {
    const model = JOURNAL_READ_ONLY_FIXTURE.model;
    expect(model.access.mode).toBe("read_only");
    expect(model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture].access.mode).toBe(
      "read_only",
    );
    expect(model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes].access.mode).toBe(
      "read_only",
    );
    expect(
      model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture].targets.every(
        (target) => !target.enabled && Boolean(target.unavailableReason),
      ),
    ).toBe(true);
  });

  it("provides a deterministic 60-item heavy-day timeline", () => {
    const timeline = JOURNAL_HEAVY_DAY_FIXTURE.model.widgetInputs[
      JOURNAL_WIDGET_INSTANCE_IDS.timeline
    ];
    expect(timeline.items).toHaveLength(60);
    expect(timeline.density).toBe("compact");
    expect(timeline.items[0]?.itemId).toBe("timeline:heavy-record-01");
    const starts = timeline.items.map((item) =>
      Date.parse(item.shape === "point" ? item.at : item.startAt),
    );
    expect(starts).toEqual([...starts].sort((left, right) => left - right));
  });

  it("keeps 04:30 on July 12 bound to the July 11 Journal day", () => {
    const model = JOURNAL_PRE_0500_BOUNDARY_FIXTURE.model;
    const timeline = model.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline];

    expect(model.day.localDate).toBe("2026-07-11");
    expect(model.day.dayBoundaryStart).toBe("05:00");
    expect(model.day.now).toBe(PRE_0500_NOW);
    expect(Date.parse(PRE_0500_NOW)).toBeLessThan(Date.parse(model.day.windowEnd));
    const lastItem = timeline.items[timeline.items.length - 1];
    expect(lastItem?.itemId).toBe("timeline:pre-boundary-capture");
    if (lastItem?.shape !== "point") throw new Error("Expected the boundary fixture to end in a point");
    expect(lastItem.at).toBe("2026-07-12T04:25:00-04:00");
    expect(timeline.items.filter((item) => item.mutability === "editable")).toHaveLength(0);
  });

  it("exports every requested named state", () => {
    expect(Object.keys(JOURNAL_STATE_FIXTURES)).toEqual([
      "loading",
      "empty",
      "stale",
      "offline",
      "readOnly",
      "error",
      "heavyDay",
      "pre0500Boundary",
    ]);
  });
});
