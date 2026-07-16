import type {
  DateSelectArg,
  DatesSetArg,
  EventApi,
  EventClickArg,
  EventContentArg,
  EventDropArg,
  EventInput,
  EventMountArg,
  ViewMountArg,
} from "@fullcalendar/core";
import dayGridPlugin from "@fullcalendar/daygrid";
import interactionPlugin, {
  type EventResizeDoneArg,
} from "@fullcalendar/interaction";
import listPlugin from "@fullcalendar/list";
import luxonPlugin from "@fullcalendar/luxon3";
import FullCalendar from "@fullcalendar/react";
import timeGridPlugin from "@fullcalendar/timegrid";
import { useEffect, useMemo, useRef } from "react";

import type {
  CalendarPlacement,
  CalendarSurfaceIntent,
  CalendarSurfaceIntentResult,
  CalendarSurfaceModel,
  CalendarSurfaceView,
} from "../contracts";
import {
  CalendarItemContent,
  calendarItemAccessibleLabel,
} from "./CalendarItemContent";
import {
  toCalendarEngineEventInputs,
  type FullCalendarBoundaryMetadata,
} from "./toFullCalendarEventInputs";
import "./calendar-fullcalendar-bridge.css";

const STANDARD_PLUGINS = [
  timeGridPlugin,
  dayGridPlugin,
  listPlugin,
  interactionPlugin,
  luxonPlugin,
];
const POINT_RENDER_DURATION = "00:20:00";
const ONE_HOUR_SECONDS = 60 * 60;
const LOGICAL_DAY_LIST_VIEW = "wbLogicalDayList";

const usesLogicalDayList = (view: CalendarSurfaceView): boolean =>
  view.presentation === "list" && view.range === "day";

const engineView = (view: CalendarSurfaceView): string => {
  if (view.presentation === "list") {
    if (view.range === "week") return "listWeek";
    if (view.range === "month") return "listMonth";
    return LOGICAL_DAY_LIST_VIEW;
  }
  if (view.range === "week") return "timeGridWeek";
  if (view.range === "month") return "dayGridMonth";
  return "timeGridDay";
};

const metadata = (arg: EventContentArg | EventMountArg): FullCalendarBoundaryMetadata =>
  arg.event.extendedProps as FullCalendarBoundaryMetadata;

const secondsFromLocalDate = (iso: string, localDate: string): number => {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(iso);
  const baseMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(localDate);
  if (!match || !baseMatch) return 0;

  const dateUtc = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const baseUtc = Date.UTC(
    Number(baseMatch[1]),
    Number(baseMatch[2]) - 1,
    Number(baseMatch[3]),
  );
  const dayOffset = Math.round((dateUtc - baseUtc) / 86_400_000);
  return (
    dayOffset * 86_400 +
    Number(match[4]) * 3_600 +
    Number(match[5]) * 60 +
    Number(match[6] ?? 0)
  );
};

const calendarDuration = (totalSeconds: number): string => {
  const bounded = Math.max(0, totalSeconds);
  const hours = Math.floor(bounded / 3_600);
  const minutes = Math.floor((bounded % 3_600) / 60);
  const seconds = Math.floor(bounded % 60);
  return [hours, minutes, seconds]
    .map((part) => part.toString().padStart(2, "0"))
    .join(":");
};

export function calendarWindowOptions(model: CalendarSurfaceModel) {
  const slotMinSeconds = secondsFromLocalDate(
    model.visibleRange.start,
    model.selectedDate,
  );
  const slotMaxSeconds = secondsFromLocalDate(
    model.visibleRange.endExclusive,
    model.selectedDate,
  );
  const timedStarts = model.items.flatMap((item) => {
    if (item.placement.shape === "all_day") return [];
    return [
      secondsFromLocalDate(
        item.placement.shape === "point"
          ? item.placement.at
          : item.placement.startAt,
        model.selectedDate,
      ),
    ];
  });
  const earliestStart =
    timedStarts.length > 0 ? Math.min(...timedStarts) : slotMinSeconds;
  const scrollSeconds = Math.max(slotMinSeconds, earliestStart - ONE_HOUR_SECONDS);
  return {
    slotMinTime: calendarDuration(slotMinSeconds),
    slotMaxTime: calendarDuration(slotMaxSeconds),
    scrollTime: calendarDuration(scrollSeconds),
  } as const;
}

export function calendarRangeRequestBounds(
  model: CalendarSurfaceModel,
  engineRange: Pick<DatesSetArg, "startStr" | "endStr">,
) {
  return usesLogicalDayList(model.view)
    ? {
        start: model.visibleRange.start,
        endExclusive: model.visibleRange.endExclusive,
      }
    : { start: engineRange.startStr, endExclusive: engineRange.endStr };
}

const placementFromEvent = (
  event: EventApi,
  original: CalendarPlacement,
): CalendarPlacement | null => {
  if (!event.startStr) return null;
  if (original.shape === "point") return { shape: "point", at: event.startStr };
  if (original.shape === "all_day") {
    if (!event.endStr) return null;
    return {
      shape: "all_day",
      startDate: event.startStr.slice(0, 10),
      endDateExclusive: event.endStr.slice(0, 10),
    };
  }
  if (!event.endStr) return null;
  return { shape: "span", startAt: event.startStr, endAt: event.endStr };
};

const placementFromSelection = (selection: DateSelectArg): CalendarPlacement =>
  selection.allDay
    ? {
        shape: "all_day",
        startDate: selection.startStr.slice(0, 10),
        endDateExclusive: selection.endStr.slice(0, 10),
      }
    : {
        shape: "span",
        startAt: selection.startStr,
        endAt: selection.endStr,
      };

export interface FullCalendarSurfaceAdapterProps {
  readonly model: CalendarSurfaceModel;
  readonly density?: "comfortable" | "compact";
  onIntent(
    intent: CalendarSurfaceIntent,
  ): CalendarSurfaceIntentResult | Promise<CalendarSurfaceIntentResult>;
  onAnnouncement?(message: string, tone: "polite" | "assertive"): void;
  onItemActivate?(item: CalendarSurfaceModel["items"][number], triggerElement: HTMLElement): void;
  createRequestId?(): string;
}

/**
 * Private rendering-engine boundary. FullCalendar models and callbacks stop here;
 * the candidate surface speaks only Work Buddy models, intents, and results.
 */
export function FullCalendarSurfaceAdapter({
  model,
  density = "comfortable",
  onIntent,
  onAnnouncement,
  onItemActivate,
  createRequestId,
}: FullCalendarSurfaceAdapterProps) {
  const calendarRef = useRef<FullCalendar | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const requestSequenceRef = useRef(0);
  const lastRangeRef = useRef("");
  const itemById = useMemo(
    () => new Map(model.items.map((item) => [item.id, item] as const)),
    [model.items],
  );
  const events = useMemo(
    () => toCalendarEngineEventInputs(model) as EventInput[],
    [model],
  );
  const windowOptions = useMemo(() => calendarWindowOptions(model), [model]);
  const readOnly = model.access.mode === "read_only";
  const canCreate = !readOnly && model.capabilities?.create === true;

  const nextRequestId = () => {
    if (createRequestId) return createRequestId();
    requestSequenceRef.current += 1;
    return `calendar-surface:${model.revision}:${requestSequenceRef.current}`;
  };

  useEffect(() => {
    const api = calendarRef.current?.getApi();
    if (!api) return;
    const nextView = engineView(model.view);
    if (api.view.type !== nextView) api.changeView(nextView);
    api.gotoDate(model.selectedDate);
  }, [model.selectedDate, model.view.presentation, model.view.range]);

  useEffect(() => {
    const container = containerRef.current;
    const calendar = calendarRef.current;
    if (!container || !calendar || typeof ResizeObserver === "undefined") return;

    let animationFrame: number | undefined;
    let resizeCount = 0;
    const observer = new ResizeObserver(() => {
      if (animationFrame !== undefined) cancelAnimationFrame(animationFrame);
      animationFrame = requestAnimationFrame(() => {
        resizeCount += 1;
        container.dataset.wbCalendarResizeCount = resizeCount.toString();
        calendar.getApi().updateSize();
      });
    });
    observer.observe(container);
    return () => {
      observer.disconnect();
      if (animationFrame !== undefined) cancelAnimationFrame(animationFrame);
    };
  }, []);

  const announceFailure = (result: CalendarSurfaceIntentResult) => {
    onAnnouncement?.(
      result.message ?? "The calendar change could not be saved and was restored.",
      "assertive",
    );
  };

  const dispatchMutation = async (
    intent: CalendarSurfaceIntent,
    revert: () => void,
  ) => {
    try {
      const result = await onIntent(intent);
      if (result.status !== "accepted") {
        revert();
        announceFailure(result);
      }
    } catch {
      revert();
      announceFailure({
        status: "unavailable",
        message: "The calendar provider was unavailable and the change was restored.",
      });
    }
  };

  const dispatchNonMutation = (intent: CalendarSurfaceIntent) => {
    void Promise.resolve(onIntent(intent))
      .then((result) => {
        if (result.status !== "accepted") announceFailure(result);
      })
      .catch(() => {
        announceFailure({
          status: "unavailable",
          message: "The calendar provider was unavailable.",
        });
      });
  };

  const handleDatesSet = (range: DatesSetArg) => {
    // FullCalendar's list view is civil-day based. Its custom visibleRange is
    // deliberately widened to every civil date touched by a Work Buddy logical
    // day, but that engine-only widening must never leak through our API.
    const { start: requestedStart, endExclusive: requestedEnd } =
      calendarRangeRequestBounds(model, range);
    const key = `${model.view.presentation}:${model.view.range}:${requestedStart}:${requestedEnd}:${model.timezone}`;
    if (lastRangeRef.current === key) return;
    lastRangeRef.current = key;
    dispatchNonMutation({
      type: "calendar.range-requested",
      view: model.view,
      start: requestedStart,
      endExclusive: requestedEnd,
      timezone: model.timezone,
    });
  };

  const handleEventClick = (arg: EventClickArg) => {
    const item = itemById.get(arg.event.id);
    if (!item?.capabilities.open) return;
    if (onItemActivate) {
      onItemActivate(item, arg.el);
      return;
    }
    dispatchNonMutation({ type: "calendar.item-open-requested", itemId: item.id });
  };

  const handleSelect = (selection: DateSelectArg) => {
    if (!canCreate) return;
    dispatchNonMutation({
      type: "calendar.item-create-requested",
      requestId: nextRequestId(),
      placement: placementFromSelection(selection),
      sourceId: model.sources[0]?.sourceId,
    });
    calendarRef.current?.getApi().unselect();
  };

  const handleEventDrop = (arg: EventDropArg) => {
    const item = itemById.get(arg.event.id);
    if (!item || readOnly || !item.capabilities.move) {
      arg.revert();
      return;
    }
    const placement = placementFromEvent(arg.event, item.placement);
    if (!placement) {
      arg.revert();
      return;
    }
    void dispatchMutation(
      {
        type: "calendar.item-move-requested",
        requestId: nextRequestId(),
        itemId: item.id,
        expectedRevision: item.revision,
        placement,
      },
      arg.revert,
    );
  };

  const handleEventResize = (arg: EventResizeDoneArg) => {
    const item = itemById.get(arg.event.id);
    if (!item || readOnly || !item.capabilities.resize) {
      arg.revert();
      return;
    }
    const placement = placementFromEvent(arg.event, item.placement);
    if (!placement) {
      arg.revert();
      return;
    }
    void dispatchMutation(
      {
        type: "calendar.item-resize-requested",
        requestId: nextRequestId(),
        itemId: item.id,
        expectedRevision: item.revision,
        placement,
      },
      arg.revert,
    );
  };

  const mountEvent = (arg: EventMountArg) => {
    const item = itemById.get(metadata(arg).itemId);
    if (!item) return;
    arg.el.dataset.wbCalendarItemId = item.id;
    arg.el.setAttribute("aria-label", calendarItemAccessibleLabel(item, model.timezone));
    if (item.capabilities.open) {
      arg.el.setAttribute("role", "button");
      if (onItemActivate) arg.el.setAttribute("aria-haspopup", "dialog");
    }
  };

  const mountView = (arg: ViewMountArg) => {
    arg.el
      .querySelectorAll<HTMLElement>("[data-wb-calendar-scroll-owner]")
      .forEach((element) => element.removeAttribute("data-wb-calendar-scroll-owner"));
    const scrollOwner =
      arg.el.querySelector<HTMLElement>(".fc-scroller-liquid-absolute") ??
      arg.el.querySelector<HTMLElement>(".fc-scroller");
    scrollOwner?.setAttribute("data-wb-calendar-scroll-owner", "");
  };

  return (
    <div
      ref={containerRef}
      className={`wb-calendar-surface wb-calendar-surface--${density}`}
      role="region"
      aria-label={`Calendar surface for ${model.selectedDate}`}
      data-wb-calendar-surface="fullcalendar"
      data-wb-calendar-view={`${model.view.presentation}:${model.view.range}`}
      data-wb-calendar-logical-day-list={
        usesLogicalDayList(model.view) ? "" : undefined
      }
    >
      <FullCalendar
        ref={calendarRef}
        plugins={STANDARD_PLUGINS}
        views={{
          [LOGICAL_DAY_LIST_VIEW]: {
            type: "list",
          },
        }}
        initialView={engineView(model.view)}
        initialDate={model.selectedDate}
        visibleRange={
          usesLogicalDayList(model.view)
            ? {
                start: model.visibleRange.start,
                end: model.visibleRange.endExclusive,
              }
            : undefined
        }
        timeZone={model.timezone}
        events={events}
        headerToolbar={false}
        footerToolbar={false}
        allDaySlot={model.items.some((item) => item.placement.shape === "all_day")}
        height="100%"
        expandRows={false}
        handleWindowResize={false}
        now={model.now}
        nowIndicator
        slotMinTime={windowOptions.slotMinTime}
        slotMaxTime={windowOptions.slotMaxTime}
        scrollTime={windowOptions.scrollTime}
        scrollTimeReset={false}
        slotDuration={density === "compact" ? "00:30:00" : "00:15:00"}
        slotLabelInterval="01:00:00"
        slotLabelFormat={{ hour: "numeric", minute: "2-digit", meridiem: "short" }}
        defaultTimedEventDuration={POINT_RENDER_DURATION}
        forceEventDuration={false}
        displayEventTime={false}
        eventDisplay="block"
        eventInteractive
        editable={!readOnly}
        selectable={canCreate}
        selectMirror
        eventResizableFromStart
        eventOverlap
        slotEventOverlap={false}
        eventMinHeight={density === "compact" ? 32 : 40}
        eventShortHeight={density === "compact" ? 52 : 64}
        eventMaxStack={4}
        moreLinkClick="popover"
        eventOrder="start,-duration,title"
        datesSet={handleDatesSet}
        select={handleSelect}
        eventDrop={handleEventDrop}
        eventResize={handleEventResize}
        eventContent={(arg) => {
          const item = itemById.get(metadata(arg).itemId);
          return item ? (
            <CalendarItemContent item={item} timezone={model.timezone} />
          ) : null;
        }}
        eventClick={handleEventClick}
        eventDidMount={mountEvent}
        viewDidMount={mountView}
      />
    </div>
  );
}
