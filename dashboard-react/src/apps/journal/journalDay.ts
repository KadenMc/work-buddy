import type { JournalDayBinding } from "./contracts";

const LOCAL_DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/u;
const BOUNDARY_PATTERN = /^(\d{1,2}):(\d{2})/u;
const DAY_MS = 86_400_000;

/**
 * A Journal local date names a real calendar day written as YYYY-MM-DD.
 *
 * Date inputs report partially typed and cleared values, and a query string can
 * carry anything at all. Both are read through here so an unfinished selection
 * reads as "no day chosen" rather than as a day the server has to reject.
 */
export function isJournalLocalDate(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = LOCAL_DATE_PATTERN.exec(value);
  if (match === null) return false;
  const instant = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isFinite(instant) && localDateOfInstant(instant) === value;
}

function localDateOfInstant(instant: number): string {
  return new Date(instant).toISOString().slice(0, 10);
}

function instantOfLocalDate(localDate: string): number {
  const match = LOCAL_DATE_PATTERN.exec(localDate);
  if (match === null) throw new Error("Journal received an invalid local date.");
  return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

/**
 * The reader's current Journal day, read from the day binding the server sent.
 *
 * A Journal day opens at `dayBoundaryStart` in `timezone`, so while the bound
 * window still holds `now` the shown day is today exactly. Once the reader is
 * looking at another day, the boundary is removed from `now` and the calendar
 * date is read in the day's own zone. Within an hour of the boundary on a
 * daylight-saving transition that shift can land on the neighbouring day.
 */
export function journalTodayLocalDate(day: JournalDayBinding): string {
  const now = Date.parse(day.now);
  if (!Number.isFinite(now)) return day.localDate;
  const windowStart = Date.parse(day.windowStart);
  const windowEnd = Date.parse(day.windowEnd);
  if (
    Number.isFinite(windowStart) &&
    Number.isFinite(windowEnd) &&
    now >= windowStart &&
    now < windowEnd
  ) {
    return day.localDate;
  }
  const boundary = BOUNDARY_PATTERN.exec(day.dayBoundaryStart);
  const boundaryMs = boundary === null
    ? 0
    : (Number(boundary[1]) * 60 + Number(boundary[2])) * 60_000;
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: day.timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date(now - boundaryMs));
    const part = (type: string) =>
      parts.find((candidate) => candidate.type === type)?.value ?? "";
    const candidate = `${part("year").padStart(4, "0")}-${part("month")}-${part("day")}`;
    return isJournalLocalDate(candidate) ? candidate : day.localDate;
  } catch {
    return day.localDate;
  }
}

/**
 * Move the selection by whole days, stopping at today. There is no Journal day
 * after today to read, so forward navigation rests on the current day instead
 * of walking into days that hold nothing.
 */
export function shiftJournalLocalDate(
  localDate: string,
  days: number,
  today: string,
): string {
  const shifted = localDateOfInstant(instantOfLocalDate(localDate) + days * DAY_MS);
  if (!isJournalLocalDate(today)) return shifted;
  return shifted > today ? today : shifted;
}

/** Today is the Journal's resting state, so it is the search that carries no day. */
export function journalSearchForDay(search: string, localDate: string | null): string {
  const query = new URLSearchParams(search);
  if (isJournalLocalDate(localDate)) query.set("day", localDate);
  else query.delete("day");
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

/** The day the URL asks for, or null when it asks for no particular day. */
export function journalDayFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("day");
  return isJournalLocalDate(value) ? value : null;
}

/** True while the URL names a day at all, including one that cannot be read. */
export function journalSearchHasDay(search: string): boolean {
  return new URLSearchParams(search).has("day");
}
