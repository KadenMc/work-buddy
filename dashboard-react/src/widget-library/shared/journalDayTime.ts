import { DateTime } from "luxon";

export interface JournalDayTimeContext {
  readonly localDate: string;
  readonly timezone: string;
  readonly windowStart: string;
  readonly windowEnd: string;
}

const LOCAL_TIME = /^(?:[01]\d|2[0-3]):[0-5]\d$/u;

/**
 * Resolve a wall-clock time inside a backend-authored Journal window.
 *
 * The first candidate uses the Journal day's civil date. Times before the
 * configured day boundary belong to the following civil date. During a fold
 * we explicitly choose the earlier possible instant. A nonexistent DST-gap
 * time is rejected so the stored occurrence never differs from user input.
 */
export function journalInstantAtLocalTime(
  day: JournalDayTimeContext,
  localTime: string,
): string {
  if (!LOCAL_TIME.test(localTime)) {
    throw new Error("Enter a valid Journal time.");
  }
  const start = DateTime.fromISO(day.windowStart, { setZone: true });
  const end = DateTime.fromISO(day.windowEnd, { setZone: true });
  const civilDate = DateTime.fromISO(day.localDate, { zone: day.timezone });
  if (!start.isValid || !end.isValid || !civilDate.isValid) {
    throw new Error("The Journal day window is unavailable.");
  }

  const resolve = (localDate: string): DateTime => {
    const candidate = DateTime.fromISO(`${localDate}T${localTime}`, {
      zone: day.timezone,
      setZone: true,
    });
    if (!candidate.isValid) throw new Error("That time is unavailable in the Journal timezone.");
    if (candidate.toFormat("HH:mm") !== localTime) {
      throw new Error("That time does not exist in the Journal timezone because of daylight saving time.");
    }
    const possible = candidate.getPossibleOffsets();
    return possible.reduce(
      (earliest, value) => value.toMillis() < earliest.toMillis() ? value : earliest,
      candidate,
    );
  };

  let candidate = resolve(day.localDate);
  if (candidate.toMillis() < start.toMillis()) {
    const nextDate = civilDate.plus({ days: 1 }).toISODate();
    if (nextDate === null) throw new Error("The Journal day window is unavailable.");
    candidate = resolve(nextDate);
  }
  if (candidate.toMillis() < start.toMillis() || candidate.toMillis() >= end.toMillis()) {
    throw new Error("Choose a time inside this Journal day.");
  }
  const result = candidate.toISO({ suppressMilliseconds: true });
  if (result === null) throw new Error("That Journal time could not be resolved.");
  return result;
}

export function journalLocalTimeForInstant(instant: string, timezone: string): string {
  const value = DateTime.fromISO(instant, { setZone: true }).setZone(timezone);
  if (!value.isValid) return "";
  return value.toFormat("HH:mm");
}
