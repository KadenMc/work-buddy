import { formatTimeRange } from "../../shared";
import type { CalendarSurfaceItem } from "./contracts";

export const calendarItemTimeLabel = (
  item: CalendarSurfaceItem,
  timezone: string,
): string => {
  if (item.placement.shape === "all_day") return "All day";
  if (item.placement.shape === "point") {
    return formatTimeRange(item.placement.at, undefined, timezone);
  }
  return formatTimeRange(
    item.placement.startAt,
    item.placement.endAt,
    timezone,
  );
};
