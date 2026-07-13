import type {
  CalendarPlacement,
  CalendarSurfaceItem,
  CalendarSurfaceModel,
  CalendarSurfaceTone,
} from "../contracts";

export interface FullCalendarBoundaryMetadata {
  readonly itemId: string;
  readonly itemRevision: string;
  readonly point: boolean;
}

/**
 * Deliberately library-neutral structural input. It is assignable to FullCalendar's
 * EventInput inside the adapter, but no FullCalendar type crosses this pure boundary.
 */
export interface CalendarEngineEventInput {
  readonly id: string;
  readonly title: string;
  readonly start: string;
  readonly end?: string;
  readonly allDay: boolean;
  readonly editable: boolean;
  readonly startEditable: boolean;
  readonly durationEditable: boolean;
  readonly classNames: string[];
  readonly extendedProps: FullCalendarBoundaryMetadata;
}

const placementDates = (
  placement: CalendarPlacement,
): Pick<CalendarEngineEventInput, "start" | "end" | "allDay"> => {
  if (placement.shape === "point") {
    return { start: placement.at, allDay: false };
  }
  if (placement.shape === "span") {
    return { start: placement.startAt, end: placement.endAt, allDay: false };
  }
  return {
    start: placement.startDate,
    end: placement.endDateExclusive,
    allDay: true,
  };
};

const safeTone = (
  item: CalendarSurfaceItem,
  sourceTones: ReadonlyMap<string, CalendarSurfaceTone>,
): CalendarSurfaceTone => item.appearance?.tone ?? sourceTones.get(item.sourceId) ?? "data-1";

export function toCalendarEngineEventInputs(
  model: CalendarSurfaceModel,
): CalendarEngineEventInput[] {
  const readOnly = model.access.mode === "read_only";
  const sourceTones = new Map(
    model.sources.map((source) => [source.sourceId, source.tone] as const),
  );

  return model.items.map((item) => {
    const canMove = !readOnly && item.capabilities.move;
    const canResize =
      !readOnly && item.placement.shape === "span" && item.capabilities.resize;
    const tone = safeTone(item, sourceTones);
    return {
      id: item.id,
      title: item.title,
      ...placementDates(item.placement),
      editable: canMove || canResize,
      startEditable: canMove,
      durationEditable: canResize,
      classNames: [
        "wb-calendar-event",
        `wb-calendar-event--${item.kind}`,
        `wb-calendar-event--${item.status.toLowerCase().replace(/[^a-z0-9-]+/g, "-")}`,
        `wb-calendar-event--${item.placement.shape.replace("_", "-")}`,
        `wb-calendar-event--tone-${tone}`,
        `wb-calendar-event--${item.appearance?.emphasis ?? "normal"}`,
        ...(canMove || canResize ? ["wb-calendar-event--editable"] : ["wb-calendar-event--fixed"]),
      ],
      extendedProps: {
        itemId: item.id,
        itemRevision: item.revision,
        point: item.placement.shape === "point",
      },
    };
  });
}
