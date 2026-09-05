import { describe, expect, it } from "vitest";

import {
  journalInstantAtLocalTime,
  journalLocalTimeForInstant,
} from "./journalDayTime";

describe("Journal day wall-clock time", () => {
  it("places pre-boundary times on the following civil date", () => {
    expect(journalInstantAtLocalTime({
      localDate: "2026-07-11",
      timezone: "America/New_York",
      windowStart: "2026-07-11T05:00:00-04:00",
      windowEnd: "2026-07-12T05:00:00-04:00",
    }, "02:15")).toBe("2026-07-12T02:15:00-04:00");
  });

  it("rejects a DST gap and chooses the earlier instant in a fold", () => {
    expect(() => journalInstantAtLocalTime({
      localDate: "2026-03-07",
      timezone: "America/New_York",
      windowStart: "2026-03-07T05:00:00-05:00",
      windowEnd: "2026-03-08T05:00:00-04:00",
    }, "02:30")).toThrow(/does not exist.*daylight saving time/i);

    expect(journalInstantAtLocalTime({
      localDate: "2026-10-31",
      timezone: "America/New_York",
      windowStart: "2026-10-31T05:00:00-04:00",
      windowEnd: "2026-11-01T05:00:00-05:00",
    }, "01:30")).toBe("2026-11-01T01:30:00-04:00");
  });

  it("formats an existing occurrence in the requested Journal timezone", () => {
    expect(journalLocalTimeForInstant("2026-07-11T15:45:00Z", "America/New_York"))
      .toBe("11:45");
  });
});
