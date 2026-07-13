import { CalendarBlank } from "@phosphor-icons/react/CalendarBlank";
import { CheckCircle } from "@phosphor-icons/react/CheckCircle";
import { Circle } from "@phosphor-icons/react/Circle";
import { ClockCountdown } from "@phosphor-icons/react/ClockCountdown";

import type { CalendarSurfaceItem } from "../contracts";
import { calendarItemTimeLabel } from "../format";

const itemIcon = (item: CalendarSurfaceItem) => {
  if (item.status === "completed") return <CheckCircle weight="fill" />;
  if (item.kind === "calendar") return <CalendarBlank weight="duotone" />;
  if (item.kind === "plan") return <ClockCountdown weight="duotone" />;
  return <Circle weight="fill" />;
};

export function calendarItemAccessibleLabel(
  item: CalendarSurfaceItem,
  timezone: string,
): string {
  return [
    item.title,
    calendarItemTimeLabel(item, timezone),
    item.kindLabel ?? item.kind,
    item.status,
    item.policy?.label ??
      (item.capabilities.move || item.capabilities.resize ? "editable" : "fixed"),
    item.provenance.label,
  ].join(", ");
}

export function CalendarItemContent({
  item,
  timezone,
}: {
  readonly item: CalendarSurfaceItem;
  readonly timezone: string;
}) {
  const time = calendarItemTimeLabel(item, timezone);
  return (
    <span className="wb-calendar-event__content" aria-hidden="true">
      <span className="wb-calendar-event__icon" aria-hidden="true">
        {itemIcon(item)}
      </span>
      <span className="wb-calendar-event__copy">
        <span className="wb-calendar-event__micro">
          <span className="wb-calendar-event__micro-time">{time}</span>
          <strong>{item.title}</strong>
        </span>
        <span className="wb-calendar-event__full">
          <span className="wb-calendar-event__eyebrow">
            <span>{time}</span>
            <span>{item.kindLabel ?? item.kind}</span>
          </span>
          <strong className="wb-calendar-event__title">{item.title}</strong>
          {item.detail ? (
            <span className="wb-calendar-event__detail">{item.detail}</span>
          ) : null}
        </span>
      </span>
    </span>
  );
}
