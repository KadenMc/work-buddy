import type {
  DayTimelineInput,
  DayTimelineItem,
  TimelineItemKind,
} from "../contracts";
import type {
  CalendarPlacement,
  CalendarSurfaceItem,
  CalendarSurfaceModel,
  CalendarSurfaceSource,
  CalendarSurfaceTone,
} from "./contracts";

const SOURCE_BY_KIND = {
  record: { sourceId: "journal", label: "Journal", tone: "data-1" },
  calendar: { sourceId: "calendar", label: "Calendar", tone: "data-2" },
  plan: { sourceId: "planner", label: "Planner", tone: "data-5" },
} as const satisfies Readonly<
  Record<TimelineItemKind, CalendarSurfaceSource>
>;

const placementFor = (item: DayTimelineItem): CalendarPlacement =>
  item.shape === "point"
    ? { shape: "point", at: item.at }
    : { shape: "span", startAt: item.startAt, endAt: item.endAt };

const toneFor = (kind: TimelineItemKind): CalendarSurfaceTone =>
  SOURCE_BY_KIND[kind].tone;

const policyFor = (item: DayTimelineItem): CalendarSurfaceItem["policy"] => {
  if (item.mutability === "past_protected") {
    return {
      label: "past — protected",
      description: "Observed history is not rescheduled from the timeline.",
    };
  }
  if (item.mutability === "fixed") {
    return {
      label: "fixed commitment",
      description: "The owning provider has not made this commitment editable here.",
    };
  }
  return { label: "editable" };
};

export function toCalendarSurfaceItem(
  item: DayTimelineItem,
  revision: string,
): CalendarSurfaceItem {
  const source = SOURCE_BY_KIND[item.kind];
  return {
    id: item.itemId,
    revision,
    sourceId: source.sourceId,
    placement: placementFor(item),
    kind: item.kind,
    title: item.title,
    ...(item.detail === undefined ? {} : { detail: item.detail }),
    status: item.status,
    provenance: item.provenance,
    capabilities: {
      open: true,
      // Journal Wave 1 promotes the proven renderer and inspector without claiming
      // provider mutations that the current Timeline intent contract cannot fulfill.
      move: false,
      resize: false,
      remove: false,
    },
    policy: policyFor(item),
    ...(item.navigation === undefined ? {} : { navigation: item.navigation }),
    appearance: {
      tone: toneFor(item.kind),
      emphasis: item.kind === "record" ? "normal" : "quiet",
    },
  };
}

/** Journal-owned data translated into the reusable, library-neutral surface model. */
export function toCalendarSurfaceModel(input: DayTimelineInput): CalendarSurfaceModel {
  const sourceIds = new Set(input.items.map((item) => SOURCE_BY_KIND[item.kind].sourceId));
  return {
    revision: input.revision,
    timezone: input.day.timezone,
    now: input.day.now,
    selectedDate: input.day.localDate,
    view: {
      range: "day",
      presentation: input.renderMode === "list" ? "list" : "calendar",
    },
    visibleRange: {
      start: input.day.windowStart,
      endExclusive: input.day.windowEnd,
    },
    access: input.access ?? { mode: "read_write" },
    capabilities: { create: false },
    sources: Object.values(SOURCE_BY_KIND).filter((source) =>
      sourceIds.has(source.sourceId),
    ),
    items: input.items.map((item) => toCalendarSurfaceItem(item, input.revision)),
  };
}
