import type {
  CalendarSurfaceItem,
  CalendarSurfaceItemCapabilities,
  CalendarSurfaceModel,
  CalendarSurfaceSource,
} from "../../widget-library/timeline/calendar-surface/contracts";

const READ_ONLY_CAPABILITIES = {
  open: true,
  move: false,
  resize: false,
  remove: false,
} as const satisfies CalendarSurfaceItemCapabilities;

const EDITABLE_CAPABILITIES = {
  open: true,
  move: true,
  resize: true,
  remove: true,
} as const satisfies CalendarSurfaceItemCapabilities;

const COMMON_SOURCES = [
  { sourceId: "journal", label: "Journal", tone: "data-1" },
  { sourceId: "planner", label: "Plans", tone: "data-2" },
  { sourceId: "work-calendar", label: "Work calendar", tone: "data-3" },
] as const satisfies readonly CalendarSurfaceSource[];

const MIXED_SOURCES = [
  ...COMMON_SOURCES,
  { sourceId: "personal-calendar", label: "Personal calendar", tone: "data-4" },
] as const satisfies readonly CalendarSurfaceSource[];

type FixtureItemInput = Omit<CalendarSurfaceItem, "revision"> & {
  readonly revision?: string;
};

function fixtureItem({ revision, ...input }: FixtureItemInput): CalendarSurfaceItem {
  return {
    ...input,
    revision: revision ?? `calendar-spike:item:${input.id}:r1`,
  };
}

interface FixtureModelInput {
  readonly fixtureId: string;
  readonly timezone: string;
  readonly selectedDate: string;
  readonly now: string;
  readonly rangeStart: string;
  readonly rangeEndExclusive: string;
  readonly items: readonly CalendarSurfaceItem[];
  readonly sources?: readonly CalendarSurfaceSource[];
  readonly access?: CalendarSurfaceModel["access"];
}

function fixtureModel({
  fixtureId,
  timezone,
  selectedDate,
  now,
  rangeStart,
  rangeEndExclusive,
  items,
  sources = COMMON_SOURCES,
  access = { mode: "read_write" },
}: FixtureModelInput): CalendarSurfaceModel {
  return {
    revision: `calendar-spike:${fixtureId}:r1`,
    timezone,
    now,
    selectedDate,
    view: { range: "day", presentation: "calendar" },
    visibleRange: { start: rangeStart, endExclusive: rangeEndExclusive },
    access,
    sources,
    items,
  };
}

const JULY11_ITEMS = [
  fixtureItem({
    id: "research-session",
    sourceId: "journal",
    placement: {
      shape: "span",
      startAt: "2026-07-11T09:05:00-04:00",
      endAt: "2026-07-11T10:17:00-04:00",
    },
    kind: "record",
    title: "Mapped Journal data contracts",
    detail: "observed session · 1h 12m",
    status: "observed",
    provenance: {
      source: "conversation_observability",
      label: "observed session",
    },
    capabilities: READ_ONLY_CAPABILITIES,
    navigation: { targetType: "session", targetId: "session:journal-contracts" },
    appearance: { tone: "data-1", emphasis: "normal" },
  }),
  fixtureItem({
    id: "product-standup",
    sourceId: "work-calendar",
    placement: {
      shape: "span",
      startAt: "2026-07-11T10:30:00-04:00",
      endAt: "2026-07-11T11:48:00-04:00",
    },
    kind: "calendar",
    title: "Product stand-up",
    detail: "calendar · ran 18m long",
    status: "observed",
    provenance: { source: "calendar", label: "Work calendar" },
    capabilities: READ_ONLY_CAPABILITIES,
    navigation: {
      targetType: "calendar_event",
      targetId: "calendar:product-standup",
    },
    appearance: { tone: "data-3", emphasis: "normal" },
  }),
  fixtureItem({
    id: "mobile-edge-capture",
    sourceId: "journal",
    placement: { shape: "point", at: "2026-07-11T11:51:00-04:00" },
    kind: "record",
    title: "Captured mobile timeline edge case",
    detail: "you · exact text preserved",
    status: "observed",
    provenance: { source: "user", label: "you" },
    capabilities: READ_ONLY_CAPABILITIES,
    navigation: {
      targetType: "journal_item",
      targetId: "capture:mobile-edge-case",
    },
    appearance: { tone: "data-1", emphasis: "strong" },
  }),
  fixtureItem({
    id: "prototype-mobile",
    sourceId: "planner",
    placement: {
      shape: "span",
      startAt: "2026-07-11T12:20:00-04:00",
      endAt: "2026-07-11T13:30:00-04:00",
    },
    kind: "plan",
    title: "Prototype mobile timeline",
    detail: "planned · 70m",
    status: "planned",
    provenance: { source: "planner", label: "planned" },
    capabilities: EDITABLE_CAPABILITIES,
    navigation: { targetType: "task", targetId: "task:prototype-mobile-timeline" },
    appearance: { tone: "data-2", emphasis: "strong" },
  }),
  fixtureItem({
    id: "review-tracker-schema",
    sourceId: "planner",
    placement: {
      shape: "span",
      startAt: "2026-07-11T13:30:00-04:00",
      endAt: "2026-07-11T13:55:00-04:00",
    },
    kind: "plan",
    title: "Review tracker schema",
    detail: "planned · 25m",
    status: "planned",
    provenance: { source: "planner", label: "planned" },
    capabilities: EDITABLE_CAPABILITIES,
    navigation: { targetType: "task", targetId: "task:review-tracker-schema" },
    appearance: { tone: "data-2", emphasis: "normal" },
  }),
  fixtureItem({
    id: "northwind-review",
    sourceId: "work-calendar",
    placement: {
      shape: "span",
      startAt: "2026-07-11T14:00:00-04:00",
      endAt: "2026-07-11T14:45:00-04:00",
    },
    kind: "calendar",
    title: "Northwind project review",
    detail: "calendar · fixed · 45m",
    status: "planned",
    provenance: { source: "calendar", label: "Work calendar" },
    capabilities: READ_ONLY_CAPABILITIES,
    navigation: {
      targetType: "calendar_event",
      targetId: "calendar:northwind-review",
    },
    appearance: { tone: "data-3", emphasis: "normal" },
  }),
] as const satisfies readonly CalendarSurfaceItem[];

export const CALENDAR_SPIKE_JULY11 = fixtureModel({
  fixtureId: "july11",
  timezone: "America/New_York",
  selectedDate: "2026-07-11",
  now: "2026-07-11T12:18:00-04:00",
  rangeStart: "2026-07-11T05:00:00-04:00",
  rangeEndExclusive: "2026-07-12T05:00:00-04:00",
  items: JULY11_ITEMS,
});

export const CALENDAR_SPIKE_EMPTY = fixtureModel({
  fixtureId: "empty",
  timezone: "America/New_York",
  selectedDate: "2026-07-11",
  now: "2026-07-11T12:18:00-04:00",
  rangeStart: "2026-07-11T05:00:00-04:00",
  rangeEndExclusive: "2026-07-12T05:00:00-04:00",
  items: [],
});

const DENSE_BASE = Date.parse("2026-07-11T05:00:00-04:00");

function denseTimestamp(offsetMinutes: number): string {
  return new Date(DENSE_BASE + offsetMinutes * 60_000).toISOString();
}

const DENSE_200_ITEMS = Array.from({ length: 200 }, (_, index) => {
  const ordinal = index + 1;
  const kind = (["record", "plan", "calendar"] as const)[index % 3]!;
  const sourceId =
    kind === "record" ? "journal" : kind === "plan" ? "planner" : "work-calendar";
  const startsAt = denseTimestamp(index * 5);
  const placement =
    index % 4 === 0
      ? ({
          shape: "span",
          startAt: startsAt,
          endAt: denseTimestamp(index * 5 + 20 + (index % 3) * 10),
        } as const)
      : ({ shape: "point", at: startsAt } as const);

  return fixtureItem({
    id: `dense-${ordinal.toString().padStart(3, "0")}`,
    sourceId,
    placement,
    kind,
    title: `Dense fixture item ${ordinal}`,
    detail: `${kind} · deterministic fixture`,
    status: kind === "record" ? "observed" : "planned",
    provenance: { source: sourceId, label: sourceId },
    capabilities: kind === "plan" ? EDITABLE_CAPABILITIES : READ_ONLY_CAPABILITIES,
    appearance: {
      tone: kind === "record" ? "data-1" : kind === "plan" ? "data-2" : "data-3",
      emphasis: index % 7 === 0 ? "strong" : "normal",
    },
  });
});

export const CALENDAR_SPIKE_DENSE_200 = fixtureModel({
  fixtureId: "dense200",
  timezone: "America/New_York",
  selectedDate: "2026-07-11",
  now: "2026-07-11T12:18:00-04:00",
  rangeStart: "2026-07-11T05:00:00-04:00",
  rangeEndExclusive: "2026-07-12T05:00:00-04:00",
  items: DENSE_200_ITEMS,
});

const OVERLAP_ITEMS = [
  fixtureItem({
    id: "overlap-observed-session",
    sourceId: "journal",
    placement: {
      shape: "span",
      startAt: "2026-07-11T09:00:00-04:00",
      endAt: "2026-07-11T11:30:00-04:00",
    },
    kind: "record",
    title: "Observed focus session",
    status: "observed",
    provenance: { source: "journal", label: "Journal" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-1", emphasis: "quiet" },
  }),
  fixtureItem({
    id: "overlap-editable-plan",
    sourceId: "planner",
    placement: {
      shape: "span",
      startAt: "2026-07-11T09:15:00-04:00",
      endAt: "2026-07-11T10:45:00-04:00",
    },
    kind: "plan",
    title: "Editable implementation block",
    status: "planned",
    provenance: { source: "planner", label: "Plans" },
    capabilities: EDITABLE_CAPABILITIES,
    appearance: { tone: "data-2", emphasis: "strong" },
  }),
  fixtureItem({
    id: "overlap-fixed-calendar",
    sourceId: "work-calendar",
    placement: {
      shape: "span",
      startAt: "2026-07-11T09:30:00-04:00",
      endAt: "2026-07-11T10:15:00-04:00",
    },
    kind: "calendar",
    title: "Fixed external meeting",
    status: "planned",
    provenance: { source: "calendar", label: "Work calendar" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-3", emphasis: "normal" },
  }),
  fixtureItem({
    id: "overlap-editable-calendar",
    sourceId: "work-calendar",
    placement: {
      shape: "span",
      startAt: "2026-07-11T09:45:00-04:00",
      endAt: "2026-07-11T11:00:00-04:00",
    },
    kind: "calendar",
    title: "Movable calendar hold",
    status: "planned",
    provenance: { source: "calendar", label: "Work calendar" },
    capabilities: EDITABLE_CAPABILITIES,
    appearance: { tone: "data-3", emphasis: "normal" },
  }),
  fixtureItem({
    id: "overlap-point-record",
    sourceId: "journal",
    placement: { shape: "point", at: "2026-07-11T10:00:00-04:00" },
    kind: "record",
    title: "Captured during overlap",
    status: "observed",
    provenance: { source: "user", label: "you" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-1", emphasis: "strong" },
  }),
] as const satisfies readonly CalendarSurfaceItem[];

export const CALENDAR_SPIKE_OVERLAP = fixtureModel({
  fixtureId: "overlap",
  timezone: "America/New_York",
  selectedDate: "2026-07-11",
  now: "2026-07-11T10:02:00-04:00",
  rangeStart: "2026-07-11T05:00:00-04:00",
  rangeEndExclusive: "2026-07-12T05:00:00-04:00",
  items: OVERLAP_ITEMS,
});

const CROSS_MIDNIGHT_ITEMS = [
  fixtureItem({
    id: "cross-ordinary-midnight",
    sourceId: "planner",
    placement: {
      shape: "span",
      startAt: "2026-07-11T23:30:00-04:00",
      endAt: "2026-07-12T01:15:00-04:00",
    },
    kind: "plan",
    title: "Late-night release window",
    detail: "crosses ordinary midnight",
    status: "planned",
    provenance: { source: "planner", label: "Plans" },
    capabilities: EDITABLE_CAPABILITIES,
    appearance: { tone: "data-2", emphasis: "strong" },
  }),
  fixtureItem({
    id: "cross-journal-boundary",
    sourceId: "journal",
    placement: {
      shape: "span",
      startAt: "2026-07-12T04:45:00-04:00",
      endAt: "2026-07-12T05:30:00-04:00",
    },
    kind: "record",
    title: "Session across the Journal boundary",
    detail: "crosses the configured 05:00 day boundary",
    status: "observed",
    provenance: { source: "journal", label: "Journal" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-1", emphasis: "normal" },
  }),
] as const satisfies readonly CalendarSurfaceItem[];

export const CALENDAR_SPIKE_CROSS_MIDNIGHT = fixtureModel({
  fixtureId: "crossMidnight",
  timezone: "America/New_York",
  selectedDate: "2026-07-11",
  now: "2026-07-12T00:10:00-04:00",
  rangeStart: "2026-07-11T17:00:00-04:00",
  rangeEndExclusive: "2026-07-12T08:00:00-04:00",
  items: CROSS_MIDNIGHT_ITEMS,
});

const DST_SPRING_ITEMS = [
  fixtureItem({
    id: "dst-spring-before",
    sourceId: "journal",
    placement: {
      shape: "span",
      startAt: "2026-03-08T01:10:00-05:00",
      endAt: "2026-03-08T01:45:00-05:00",
    },
    kind: "record",
    title: "Before spring-forward",
    status: "observed",
    provenance: { source: "journal", label: "Journal" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-1", emphasis: "normal" },
  }),
  fixtureItem({
    id: "dst-spring-crossing",
    sourceId: "planner",
    placement: {
      shape: "span",
      startAt: "2026-03-08T01:30:00-05:00",
      endAt: "2026-03-08T03:30:00-04:00",
    },
    kind: "plan",
    title: "Plan crossing the missing hour",
    detail: "one elapsed hour across the offset change",
    status: "planned",
    provenance: { source: "planner", label: "Plans" },
    capabilities: EDITABLE_CAPABILITIES,
    appearance: { tone: "data-2", emphasis: "strong" },
  }),
  fixtureItem({
    id: "dst-spring-after",
    sourceId: "work-calendar",
    placement: { shape: "point", at: "2026-03-08T03:10:00-04:00" },
    kind: "calendar",
    title: "First post-transition reminder",
    status: "planned",
    provenance: { source: "calendar", label: "Work calendar" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-3", emphasis: "normal" },
  }),
] as const satisfies readonly CalendarSurfaceItem[];

export const CALENDAR_SPIKE_DST_SPRING = fixtureModel({
  fixtureId: "dstSpring",
  timezone: "America/New_York",
  selectedDate: "2026-03-08",
  now: "2026-03-08T03:15:00-04:00",
  rangeStart: "2026-03-08T00:00:00-05:00",
  rangeEndExclusive: "2026-03-09T00:00:00-04:00",
  items: DST_SPRING_ITEMS,
});

const DST_FALL_ITEMS = [
  fixtureItem({
    id: "dst-fall-first-0130",
    sourceId: "journal",
    placement: { shape: "point", at: "2026-11-01T01:30:00-04:00" },
    kind: "record",
    title: "First 1:30 AM occurrence",
    detail: "EDT · UTC−04:00",
    status: "observed",
    provenance: { source: "journal", label: "Journal" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-1", emphasis: "strong" },
  }),
  fixtureItem({
    id: "dst-fall-second-0130",
    sourceId: "journal",
    placement: { shape: "point", at: "2026-11-01T01:30:00-05:00" },
    kind: "record",
    title: "Second 1:30 AM occurrence",
    detail: "EST · UTC−05:00",
    status: "observed",
    provenance: { source: "journal", label: "Journal" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-1", emphasis: "strong" },
  }),
  fixtureItem({
    id: "dst-fall-crossing",
    sourceId: "planner",
    placement: {
      shape: "span",
      startAt: "2026-11-01T01:15:00-04:00",
      endAt: "2026-11-01T01:45:00-05:00",
    },
    kind: "plan",
    title: "Plan across the repeated hour",
    detail: "90 elapsed minutes across the offset change",
    status: "planned",
    provenance: { source: "planner", label: "Plans" },
    capabilities: EDITABLE_CAPABILITIES,
    appearance: { tone: "data-2", emphasis: "normal" },
  }),
] as const satisfies readonly CalendarSurfaceItem[];

export const CALENDAR_SPIKE_DST_FALL = fixtureModel({
  fixtureId: "dstFall",
  timezone: "America/New_York",
  selectedDate: "2026-11-01",
  now: "2026-11-01T01:40:00-05:00",
  rangeStart: "2026-11-01T00:00:00-04:00",
  rangeEndExclusive: "2026-11-02T00:00:00-05:00",
  items: DST_FALL_ITEMS,
});

const READ_ONLY_ITEMS = [
  fixtureItem({
    id: "read-only-editable-plan",
    sourceId: "planner",
    placement: {
      shape: "span",
      startAt: "2026-07-11T09:00:00-04:00",
      endAt: "2026-07-11T10:00:00-04:00",
    },
    kind: "plan",
    title: "Item capabilities allow editing",
    detail: "view access must still prevent every mutation",
    status: "planned",
    provenance: { source: "planner", label: "Plans" },
    capabilities: EDITABLE_CAPABILITIES,
    appearance: { tone: "data-2", emphasis: "strong" },
  }),
  fixtureItem({
    id: "read-only-calendar",
    sourceId: "work-calendar",
    placement: {
      shape: "span",
      startAt: "2026-07-11T10:30:00-04:00",
      endAt: "2026-07-11T11:15:00-04:00",
    },
    kind: "calendar",
    title: "Read-only calendar event",
    status: "planned",
    provenance: { source: "calendar", label: "Work calendar" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-3", emphasis: "normal" },
  }),
] as const satisfies readonly CalendarSurfaceItem[];

export const CALENDAR_SPIKE_READ_ONLY = fixtureModel({
  fixtureId: "readOnly",
  timezone: "America/New_York",
  selectedDate: "2026-07-11",
  now: "2026-07-11T10:45:00-04:00",
  rangeStart: "2026-07-11T05:00:00-04:00",
  rangeEndExclusive: "2026-07-12T05:00:00-04:00",
  items: READ_ONLY_ITEMS,
  access: {
    mode: "read_only",
    reason: "Calendar spike read-only fixture",
  },
});

const MIXED_SOURCE_ITEMS = [
  fixtureItem({
    id: "mixed-observed-point",
    sourceId: "journal",
    placement: { shape: "point", at: "2026-07-11T08:15:00-04:00" },
    kind: "record",
    title: "Captured a morning observation",
    status: "observed",
    provenance: { source: "user", label: "you" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-1", emphasis: "strong" },
  }),
  fixtureItem({
    id: "mixed-observed-span",
    sourceId: "journal",
    placement: {
      shape: "span",
      startAt: "2026-07-11T08:30:00-04:00",
      endAt: "2026-07-11T09:20:00-04:00",
    },
    kind: "record",
    title: "Observed work session",
    status: "observed",
    provenance: { source: "agent", label: "observed session" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-1", emphasis: "normal" },
  }),
  fixtureItem({
    id: "mixed-editable-plan",
    sourceId: "planner",
    placement: {
      shape: "span",
      startAt: "2026-07-11T09:30:00-04:00",
      endAt: "2026-07-11T10:30:00-04:00",
    },
    kind: "plan",
    title: "Editable planning block",
    status: "planned",
    provenance: { source: "planner", label: "Plans" },
    capabilities: EDITABLE_CAPABILITIES,
    appearance: { tone: "data-2", emphasis: "strong" },
  }),
  fixtureItem({
    id: "mixed-editable-calendar",
    sourceId: "work-calendar",
    placement: {
      shape: "span",
      startAt: "2026-07-11T11:00:00-04:00",
      endAt: "2026-07-11T11:45:00-04:00",
    },
    kind: "calendar",
    title: "Editable Work calendar event",
    status: "planned",
    provenance: { source: "calendar", label: "Work calendar" },
    capabilities: EDITABLE_CAPABILITIES,
    appearance: { tone: "data-3", emphasis: "normal" },
  }),
  fixtureItem({
    id: "mixed-fixed-calendar",
    sourceId: "personal-calendar",
    placement: {
      shape: "span",
      startAt: "2026-07-11T12:00:00-04:00",
      endAt: "2026-07-11T12:30:00-04:00",
    },
    kind: "calendar",
    title: "Fixed personal calendar event",
    status: "planned",
    provenance: { source: "calendar", label: "Personal calendar" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-4", emphasis: "normal" },
  }),
  fixtureItem({
    id: "mixed-all-day-calendar",
    sourceId: "personal-calendar",
    placement: {
      shape: "all_day",
      startDate: "2026-07-11",
      endDateExclusive: "2026-07-12",
    },
    kind: "calendar",
    title: "All-day personal commitment",
    status: "planned",
    provenance: { source: "calendar", label: "Personal calendar" },
    capabilities: READ_ONLY_CAPABILITIES,
    appearance: { tone: "data-4", emphasis: "quiet" },
  }),
] as const satisfies readonly CalendarSurfaceItem[];

export const CALENDAR_SPIKE_MIXED_SOURCE = fixtureModel({
  fixtureId: "mixedSource",
  timezone: "America/New_York",
  selectedDate: "2026-07-11",
  now: "2026-07-11T10:45:00-04:00",
  rangeStart: "2026-07-11T05:00:00-04:00",
  rangeEndExclusive: "2026-07-12T05:00:00-04:00",
  items: MIXED_SOURCE_ITEMS,
  sources: MIXED_SOURCES,
});

export const CALENDAR_SPIKE_FIXTURES = {
  july11: CALENDAR_SPIKE_JULY11,
  empty: CALENDAR_SPIKE_EMPTY,
  dense200: CALENDAR_SPIKE_DENSE_200,
  overlap: CALENDAR_SPIKE_OVERLAP,
  crossMidnight: CALENDAR_SPIKE_CROSS_MIDNIGHT,
  dstSpring: CALENDAR_SPIKE_DST_SPRING,
  dstFall: CALENDAR_SPIKE_DST_FALL,
  readOnly: CALENDAR_SPIKE_READ_ONLY,
  mixedSource: CALENDAR_SPIKE_MIXED_SOURCE,
} as const satisfies Readonly<Record<string, CalendarSurfaceModel>>;

export type CalendarSpikeFixtureId = keyof typeof CALENDAR_SPIKE_FIXTURES;

export interface CalendarSpikeFixtureDefinition {
  readonly fixtureId: CalendarSpikeFixtureId;
  readonly label: string;
  readonly description: string;
  readonly model: CalendarSurfaceModel;
}

export const CALENDAR_SPIKE_FIXTURE_LIST = [
  {
    fixtureId: "july11",
    label: "July 11",
    description: "Representative Journal records, plans, calendar items, and a point capture.",
    model: CALENDAR_SPIKE_JULY11,
  },
  {
    fixtureId: "empty",
    label: "Empty",
    description: "A valid calendar projection with no items.",
    model: CALENDAR_SPIKE_EMPTY,
  },
  {
    fixtureId: "dense200",
    label: "Dense · 200 items",
    description: "Two hundred deterministic mixed items for rendering and interaction stress.",
    model: CALENDAR_SPIKE_DENSE_200,
  },
  {
    fixtureId: "overlap",
    label: "Overlapping items",
    description: "Intersecting spans and a point record with mixed capabilities.",
    model: CALENDAR_SPIKE_OVERLAP,
  },
  {
    fixtureId: "crossMidnight",
    label: "Cross-midnight",
    description: "Spans crossing ordinary midnight and the Journal's 05:00 boundary.",
    model: CALENDAR_SPIKE_CROSS_MIDNIGHT,
  },
  {
    fixtureId: "dstSpring",
    label: "DST · spring",
    description: "America/New_York's missing hour on the 2026 spring transition.",
    model: CALENDAR_SPIKE_DST_SPRING,
  },
  {
    fixtureId: "dstFall",
    label: "DST · fall",
    description: "Distinct first and second occurrences of 01:30 during the repeated hour.",
    model: CALENDAR_SPIKE_DST_FALL,
  },
  {
    fixtureId: "readOnly",
    label: "Read-only view",
    description: "View-level read-only access overrides item-level edit capabilities.",
    model: CALENDAR_SPIKE_READ_ONLY,
  },
  {
    fixtureId: "mixedSource",
    label: "Mixed sources",
    description: "Point, span, all-day, editable, and fixed items from four sources.",
    model: CALENDAR_SPIKE_MIXED_SOURCE,
  },
] as const satisfies readonly CalendarSpikeFixtureDefinition[];
