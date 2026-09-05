import { describe, expect, it } from "vitest";

import type { JournalDayBinding } from "./contracts";
import {
  isJournalLocalDate,
  journalDayFromSearch,
  journalSearchForDay,
  journalSearchHasDay,
  journalTodayLocalDate,
  shiftJournalLocalDate,
} from "./journalDay";

const TODAY: JournalDayBinding = {
  dayId: "journal-day:2026-07-11:America/New_York:05:00",
  localDate: "2026-07-11",
  timezone: "America/New_York",
  dayBoundaryStart: "05:00",
  windowStart: "2026-07-11T05:00:00-04:00",
  windowEnd: "2026-07-12T05:00:00-04:00",
  now: "2026-07-11T12:18:00-04:00",
};

function pastDay(now: string): JournalDayBinding {
  return {
    ...TODAY,
    dayId: "journal-day:2026-07-09:America/New_York:05:00",
    localDate: "2026-07-09",
    windowStart: "2026-07-09T05:00:00-04:00",
    windowEnd: "2026-07-10T05:00:00-04:00",
    now,
  };
}

describe("isJournalLocalDate", () => {
  it("accepts a real calendar day written as YYYY-MM-DD", () => {
    expect(isJournalLocalDate("2026-07-11")).toBe(true);
    expect(isJournalLocalDate("2024-02-29")).toBe(true);
  });

  it("rejects the values a date input reports while it is still being typed", () => {
    expect(isJournalLocalDate("")).toBe(false);
    expect(isJournalLocalDate("2026")).toBe(false);
    expect(isJournalLocalDate("2026-07")).toBe(false);
    expect(isJournalLocalDate("2026-7-1")).toBe(false);
  });

  it("rejects days the calendar does not have", () => {
    expect(isJournalLocalDate("2026-02-30")).toBe(false);
    expect(isJournalLocalDate("2026-13-01")).toBe(false);
    expect(isJournalLocalDate("2026-00-10")).toBe(false);
    expect(isJournalLocalDate("2025-02-29")).toBe(false);
  });

  it("rejects anything that is not a string", () => {
    expect(isJournalLocalDate(null)).toBe(false);
    expect(isJournalLocalDate(undefined)).toBe(false);
    expect(isJournalLocalDate(20260711)).toBe(false);
  });
});

describe("journalSearchForDay", () => {
  it("carries a chosen day", () => {
    expect(journalSearchForDay("", "2026-07-09")).toBe("?day=2026-07-09");
  });

  it("keeps every other query the view is already using", () => {
    expect(journalSearchForDay("?provider=live", "2026-07-09")).toBe(
      "?provider=live&day=2026-07-09",
    );
    expect(journalSearchForDay("?day=2026-07-09&provider=live", "2026-07-10")).toBe(
      "?day=2026-07-10&provider=live",
    );
  });

  it("drops the day rather than writing one no day can be read from", () => {
    expect(journalSearchForDay("?day=2026-07-09", null)).toBe("");
    expect(journalSearchForDay("?day=2026-07-09", "")).toBe("");
    expect(journalSearchForDay("?day=2026-07-09", "2026-07")).toBe("");
    expect(journalSearchForDay("?day=2026-07-09", "2026-02-30")).toBe("");
    expect(journalSearchForDay("?provider=live&day=2026-07-09", "")).toBe("?provider=live");
  });
});

describe("journalDayFromSearch", () => {
  it("reads the day the URL asks for", () => {
    expect(journalDayFromSearch("?day=2026-07-09")).toBe("2026-07-09");
    expect(journalDayFromSearch("?provider=live&day=2026-07-09")).toBe("2026-07-09");
  });

  it("reads no day when the URL names none", () => {
    expect(journalDayFromSearch("")).toBeNull();
    expect(journalDayFromSearch("?provider=live")).toBeNull();
  });

  it("reads no day from a value the Journal cannot answer for", () => {
    expect(journalDayFromSearch("?day=")).toBeNull();
    expect(journalDayFromSearch("?day=2026-07")).toBeNull();
    expect(journalDayFromSearch("?day=yesterday")).toBeNull();
    expect(journalDayFromSearch("?day=2026-02-30")).toBeNull();
  });
});

describe("journalSearchHasDay", () => {
  it("reports a named day even when nothing can be read from it", () => {
    expect(journalSearchHasDay("?day=2026-07-09")).toBe(true);
    expect(journalSearchHasDay("?day=")).toBe(true);
    expect(journalSearchHasDay("?day=nonsense")).toBe(true);
    expect(journalSearchHasDay("?provider=live")).toBe(false);
    expect(journalSearchHasDay("")).toBe(false);
  });
});

describe("shiftJournalLocalDate", () => {
  it("moves by whole days across month and year edges", () => {
    expect(shiftJournalLocalDate("2026-07-11", -1, "2026-07-11")).toBe("2026-07-10");
    expect(shiftJournalLocalDate("2026-07-01", -1, "2026-07-11")).toBe("2026-06-30");
    expect(shiftJournalLocalDate("2026-01-01", -1, "2026-07-11")).toBe("2025-12-31");
    expect(shiftJournalLocalDate("2024-03-01", -1, "2026-07-11")).toBe("2024-02-29");
  });

  it("moves forward while there are days left to read", () => {
    expect(shiftJournalLocalDate("2026-07-09", 1, "2026-07-11")).toBe("2026-07-10");
    expect(shiftJournalLocalDate("2026-06-30", 1, "2026-07-11")).toBe("2026-07-01");
  });

  it("rests on today instead of walking past it", () => {
    expect(shiftJournalLocalDate("2026-07-11", 1, "2026-07-11")).toBe("2026-07-11");
    expect(shiftJournalLocalDate("2026-07-10", 7, "2026-07-11")).toBe("2026-07-11");
  });

  it("still moves when today cannot be read", () => {
    expect(shiftJournalLocalDate("2026-07-10", 1, "")).toBe("2026-07-11");
  });

  it("refuses a local date it cannot read", () => {
    expect(() => shiftJournalLocalDate("yesterday", -1, "2026-07-11")).toThrow();
  });
});

describe("journalTodayLocalDate", () => {
  it("uses the bound day itself while that day still holds the observed instant", () => {
    expect(journalTodayLocalDate(TODAY)).toBe("2026-07-11");
  });

  it("reads today from the observed instant while another day is shown", () => {
    expect(journalTodayLocalDate(pastDay("2026-07-11T12:18:00-04:00"))).toBe("2026-07-11");
  });

  it("keeps the day open until its boundary passes", () => {
    expect(journalTodayLocalDate(pastDay("2026-07-11T03:30:00-04:00"))).toBe("2026-07-10");
    expect(journalTodayLocalDate(pastDay("2026-07-11T05:30:00-04:00"))).toBe("2026-07-11");
  });

  it("falls back to the bound day when the instant or zone cannot be read", () => {
    expect(journalTodayLocalDate({ ...pastDay("2026-07-11T12:18:00-04:00"), now: "soon" }))
      .toBe("2026-07-09");
    expect(journalTodayLocalDate({
      ...pastDay("2026-07-11T12:18:00-04:00"),
      timezone: "Nowhere/Imaginary",
    })).toBe("2026-07-09");
  });
});
