import {
  JOURNAL_WIDGET_INSTANCE_IDS,
  type JournalAccess,
  type JournalDataQuality,
  type JournalDayBinding,
  type JournalFixtureState,
  type JournalTimelineItem,
  type JournalViewModel,
} from "../contracts";
import {
  JULY11_DAY,
  JULY11_INITIAL_MODEL,
  JULY11_INITIAL_TIMELINE_ITEMS,
  JULY11_NOW,
} from "./july11";

function deriveModel(
  fixtureId: string,
  revision: string,
  options: {
    readonly day?: JournalDayBinding;
    readonly access?: JournalAccess;
    readonly quality?: JournalDataQuality;
    readonly timelineItems?: readonly JournalTimelineItem[];
    readonly timelineDensity?: "comfortable" | "compact";
    readonly capturesToday?: number;
    readonly captureSubmissions?: JournalViewModel["widgetInputs"]["default:capture"]["recentSubmissions"];
    readonly runningNotes?: JournalViewModel["widgetInputs"]["default:running-notes"]["items"];
    readonly disableCaptureReason?: string;
  } = {},
): JournalViewModel {
  const day = options.day ?? JULY11_INITIAL_MODEL.day;
  const access = options.access ?? JULY11_INITIAL_MODEL.access;
  const capture = JULY11_INITIAL_MODEL.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture];
  const timeline = JULY11_INITIAL_MODEL.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline];
  const runningNotes =
    JULY11_INITIAL_MODEL.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.runningNotes];

  return {
    ...JULY11_INITIAL_MODEL,
    revision,
    day,
    access,
    quality: options.quality ?? JULY11_INITIAL_MODEL.quality,
    source: { kind: "fixture", fixtureId, label: "Demo data" },
    widgetInputs: {
      [JOURNAL_WIDGET_INSTANCE_IDS.capture]: {
        ...capture,
        revision,
        dayId: day.dayId,
        access,
        targets: options.disableCaptureReason
          ? capture.targets.map((target) => ({
              ...target,
              enabled: false,
              unavailableReason: options.disableCaptureReason,
            }))
          : capture.targets,
        capturesToday: options.capturesToday ?? capture.capturesToday,
        recentSubmissions: options.captureSubmissions ?? capture.recentSubmissions,
      },
      [JOURNAL_WIDGET_INSTANCE_IDS.timeline]: {
        ...timeline,
        revision,
        day,
        density: options.timelineDensity ?? timeline.density,
        items: options.timelineItems ?? timeline.items,
      },
      [JOURNAL_WIDGET_INSTANCE_IDS.runningNotes]: {
        ...runningNotes,
        revision,
        dayId: day.dayId,
        access,
        items: options.runningNotes ?? runningNotes.items,
      },
    },
  };
}

export const JOURNAL_LOADING_FIXTURE = {
  fixtureId: "journal-loading",
  loadStatus: "loading",
  observedAt: JULY11_NOW,
  model: null,
} as const satisfies JournalFixtureState;

const EMPTY_MODEL = deriveModel("journal-empty", "journal:empty:r1", {
  timelineItems: [],
  capturesToday: 0,
  captureSubmissions: [],
  runningNotes: [],
});

export const JOURNAL_EMPTY_FIXTURE = {
  fixtureId: "journal-empty",
  loadStatus: "ready",
  observedAt: JULY11_NOW,
  model: EMPTY_MODEL,
} as const satisfies JournalFixtureState;

const STALE_OBSERVED_AT = "2026-07-11T11:48:00-04:00";
const STALE_MODEL = deriveModel("journal-stale", "journal:stale:r1", {
  quality: {
    freshness: "stale",
    observedAt: STALE_OBSERVED_AT,
    issues: [
      {
        code: "journal_snapshot_stale",
        message: "Showing the last verified Journal snapshot while data refreshes.",
        affectedInstanceIds: Object.values(JOURNAL_WIDGET_INSTANCE_IDS),
      },
    ],
  },
});

export const JOURNAL_STALE_FIXTURE = {
  fixtureId: "journal-stale",
  loadStatus: "stale",
  observedAt: STALE_OBSERVED_AT,
  model: STALE_MODEL,
} as const satisfies JournalFixtureState;

const OFFLINE_REASON = "Reconnect to Work Buddy to change the Journal.";
const OFFLINE_MODEL = deriveModel("journal-offline", "journal:offline:r1", {
  access: { mode: "read_only", reason: OFFLINE_REASON },
  quality: {
    freshness: "offline",
    observedAt: STALE_OBSERVED_AT,
    issues: [
      {
        code: "journal_provider_offline",
        message: "Showing the last verified Journal snapshot while Work Buddy is offline.",
        affectedInstanceIds: Object.values(JOURNAL_WIDGET_INSTANCE_IDS),
      },
    ],
  },
  disableCaptureReason: OFFLINE_REASON,
});

export const JOURNAL_OFFLINE_FIXTURE = {
  fixtureId: "journal-offline",
  loadStatus: "offline",
  observedAt: STALE_OBSERVED_AT,
  model: OFFLINE_MODEL,
} as const satisfies JournalFixtureState;

const READ_ONLY_REASON = "Dashboard read-only mode prevents Journal and layout changes.";
const READ_ONLY_MODEL = deriveModel("journal-read-only", "journal:read-only:r1", {
  access: { mode: "read_only", reason: READ_ONLY_REASON },
  disableCaptureReason: READ_ONLY_REASON,
});

export const JOURNAL_READ_ONLY_FIXTURE = {
  fixtureId: "journal-read-only",
  loadStatus: "ready",
  observedAt: JULY11_NOW,
  model: READ_ONLY_MODEL,
} as const satisfies JournalFixtureState;

export const JOURNAL_ERROR_FIXTURE = {
  fixtureId: "journal-error",
  loadStatus: "error",
  observedAt: JULY11_NOW,
  model: null,
  error: {
    code: "journal_fixture_failure",
    message: "The Journal fixture could not be loaded.",
    retryable: true,
  },
} as const satisfies JournalFixtureState;

function isoAtOffsetFromBoundary(offsetMinutes: number): string {
  const boundary = Date.parse(JULY11_DAY.windowStart);
  return new Date(boundary + offsetMinutes * 60_000).toISOString();
}

const HEAVY_DAY_RECORDS = Array.from({ length: 54 }, (_, index) => {
  const number = index + 1;
  return {
    itemId: `timeline:heavy-record-${number.toString().padStart(2, "0")}`,
    kind: "record",
    shape: "point",
    at: isoAtOffsetFromBoundary(15 + index * 7),
    title: `Observed activity ${number}`,
    detail: index % 3 === 0 ? "agent · derived activity" : "you · journal record",
    status: "observed",
    mutability: "past_protected",
    precision: index % 3 === 0 ? "derived" : "exact",
    provenance:
      index % 3 === 0
        ? { source: "agent", label: "agent" }
        : { source: "user", label: "you" },
  } satisfies JournalTimelineItem;
});

function timelineStart(item: JournalTimelineItem): string {
  return item.shape === "point" ? item.at : item.startAt;
}

const HEAVY_DAY_TIMELINE_ITEMS = [
  ...HEAVY_DAY_RECORDS,
  ...JULY11_INITIAL_TIMELINE_ITEMS,
].sort((left, right) => Date.parse(timelineStart(left)) - Date.parse(timelineStart(right)));

const HEAVY_DAY_MODEL = deriveModel("journal-heavy-day", "journal:heavy-day:r1", {
  timelineItems: HEAVY_DAY_TIMELINE_ITEMS,
  timelineDensity: "compact",
});

export const JOURNAL_HEAVY_DAY_FIXTURE = {
  fixtureId: "journal-heavy-day",
  loadStatus: "ready",
  observedAt: JULY11_NOW,
  model: HEAVY_DAY_MODEL,
} as const satisfies JournalFixtureState;

export const PRE_0500_NOW = "2026-07-12T04:30:00-04:00";
const PRE_0500_DAY = {
  ...JULY11_DAY,
  now: PRE_0500_NOW,
} as const satisfies JournalDayBinding;

const PRE_BOUNDARY_TIMELINE_ITEMS = [
  ...JULY11_INITIAL_TIMELINE_ITEMS.map((item) =>
    item.mutability === "editable" ? { ...item, mutability: "past_protected" as const } : item,
  ),
  {
    itemId: "timeline:pre-boundary-capture",
    kind: "record",
    shape: "point",
    at: "2026-07-12T04:25:00-04:00",
    title: "Captured before the Journal day boundary",
    detail: "you · still belongs to Saturday's Journal",
    status: "observed",
    mutability: "past_protected",
    precision: "exact",
    provenance: { source: "user", label: "you" },
  },
] as const satisfies readonly JournalTimelineItem[];

const PRE_BOUNDARY_MODEL = deriveModel(
  "journal-pre-0500-boundary",
  "journal:pre-0500-boundary:r1",
  {
    day: PRE_0500_DAY,
    timelineItems: PRE_BOUNDARY_TIMELINE_ITEMS,
  },
);

export const JOURNAL_PRE_0500_BOUNDARY_FIXTURE = {
  fixtureId: "journal-pre-0500-boundary",
  loadStatus: "ready",
  observedAt: PRE_0500_NOW,
  model: PRE_BOUNDARY_MODEL,
} as const satisfies JournalFixtureState;

export const JOURNAL_STATE_FIXTURES = {
  loading: JOURNAL_LOADING_FIXTURE,
  empty: JOURNAL_EMPTY_FIXTURE,
  stale: JOURNAL_STALE_FIXTURE,
  offline: JOURNAL_OFFLINE_FIXTURE,
  readOnly: JOURNAL_READ_ONLY_FIXTURE,
  error: JOURNAL_ERROR_FIXTURE,
  heavyDay: JOURNAL_HEAVY_DAY_FIXTURE,
  pre0500Boundary: JOURNAL_PRE_0500_BOUNDARY_FIXTURE,
} as const;
