import {
  JOURNAL_VIEW_ID,
  JOURNAL_WIDGET_INSTANCE_IDS,
  type CaptureAnnotation,
  type JournalCaptureInput,
  type JournalCaptureSubmission,
  type JournalCaptureSubmitIntent,
  type JournalCaptureTarget,
  type JournalDayBinding,
  type JournalExpectedProviderTransition,
  type JournalFixtureState,
  type JournalRunningNoteItem,
  type JournalRunningNotesInput,
  type JournalTimelineInput,
  type JournalTimelineItem,
  type JournalViewModel,
} from "../contracts";

export const JULY11_NOW = "2026-07-11T12:18:00-04:00";
export const JULY11_INITIAL_REVISION = "journal:july11:r1";
export const JULY11_SMART_PENDING_REVISION = "journal:july11:r2-smart-pending";
export const JULY11_SMART_SETTLED_REVISION = "journal:july11:r3-smart-settled";
export const JULY11_DUMB_PERSISTED_REVISION = "journal:july11:r2-dumb-persisted";

export const JULY11_DAY = {
  dayId: "journal-day:2026-07-11:America/New_York:05:00",
  localDate: "2026-07-11",
  timezone: "America/New_York",
  dayBoundaryStart: "05:00",
  windowStart: "2026-07-11T05:00:00-04:00",
  windowEnd: "2026-07-12T05:00:00-04:00",
  // Deliberately distinct from the configured 05:00 boundary.
  openedAt: "2026-07-11T08:42:00-04:00",
  now: JULY11_NOW,
} as const satisfies JournalDayBinding;

const RESEARCH_SESSION = {
  itemId: "timeline:research-session",
  kind: "record",
  shape: "span",
  startAt: "2026-07-11T09:05:00-04:00",
  endAt: "2026-07-11T10:17:00-04:00",
  title: "Mapped Journal data contracts",
  detail: "observed session · 1h 12m",
  status: "observed",
  mutability: "past_protected",
  precision: "exact",
  provenance: { source: "conversation_observability", label: "observed session" },
  navigation: { targetType: "session", targetId: "session:journal-contracts" },
} as const satisfies JournalTimelineItem;

const PRODUCT_STANDUP = {
  itemId: "timeline:product-standup",
  kind: "calendar",
  shape: "span",
  startAt: "2026-07-11T10:30:00-04:00",
  endAt: "2026-07-11T11:48:00-04:00",
  title: "Product stand-up",
  detail: "calendar · ran 18m long",
  status: "observed",
  mutability: "past_protected",
  precision: "exact",
  provenance: { source: "calendar", label: "calendar" },
  navigation: { targetType: "calendar_event", targetId: "calendar:product-standup" },
} as const satisfies JournalTimelineItem;

const MOBILE_EDGE_CAPTURE = {
  itemId: "timeline:mobile-edge-capture",
  kind: "record",
  shape: "point",
  at: "2026-07-11T11:51:00-04:00",
  title: "Captured mobile timeline edge case",
  detail: "you · exact text preserved",
  status: "observed",
  mutability: "past_protected",
  precision: "exact",
  provenance: { source: "user", label: "you" },
  navigation: { targetType: "journal_item", targetId: "capture:mobile-edge-case" },
} as const satisfies JournalTimelineItem;

const PROTOTYPE_PLAN = {
  itemId: "timeline:prototype-mobile",
  kind: "plan",
  shape: "span",
  startAt: "2026-07-11T12:20:00-04:00",
  endAt: "2026-07-11T13:30:00-04:00",
  title: "Prototype mobile timeline",
  detail: "planned · 70m",
  status: "planned",
  mutability: "editable",
  precision: "exact",
  provenance: { source: "planner", label: "planned" },
  navigation: { targetType: "task", targetId: "task:prototype-mobile-timeline" },
} as const satisfies JournalTimelineItem;

const REVIEW_PLAN = {
  itemId: "timeline:review-tracker-schema",
  kind: "plan",
  shape: "span",
  startAt: "2026-07-11T13:30:00-04:00",
  endAt: "2026-07-11T13:55:00-04:00",
  title: "Review tracker schema",
  detail: "planned · 25m",
  status: "planned",
  mutability: "editable",
  precision: "exact",
  provenance: { source: "planner", label: "planned" },
  navigation: { targetType: "task", targetId: "task:review-tracker-schema" },
} as const satisfies JournalTimelineItem;

const NORTHWIND_REVIEW = {
  itemId: "timeline:northwind-review",
  kind: "calendar",
  shape: "span",
  startAt: "2026-07-11T14:00:00-04:00",
  endAt: "2026-07-11T14:45:00-04:00",
  title: "Northwind project review",
  detail: "calendar · fixed · 45m",
  status: "planned",
  mutability: "fixed",
  precision: "exact",
  provenance: { source: "calendar", label: "calendar" },
  navigation: { targetType: "calendar_event", targetId: "calendar:northwind-review" },
} as const satisfies JournalTimelineItem;

export const JULY11_INITIAL_TIMELINE_ITEMS = [
  RESEARCH_SESSION,
  PRODUCT_STANDUP,
  MOBILE_EDGE_CAPTURE,
  PROTOTYPE_PLAN,
  REVIEW_PLAN,
  NORTHWIND_REVIEW,
] as const satisfies readonly JournalTimelineItem[];

const REVISED_PROTOTYPE_PLAN = {
  ...PROTOTYPE_PLAN,
  startAt: "2026-07-11T12:30:00-04:00",
  endAt: "2026-07-11T13:30:00-04:00",
  detail: "replanned · protected focus · 60m",
} as const satisfies JournalTimelineItem;

const REVISED_REVIEW_PLAN = {
  ...REVIEW_PLAN,
  startAt: "2026-07-11T13:35:00-04:00",
  endAt: "2026-07-11T13:55:00-04:00",
  detail: "replanned · 20m",
} as const satisfies JournalTimelineItem;

export const JULY11_REVISED_TIMELINE_ITEMS = [
  RESEARCH_SESSION,
  PRODUCT_STANDUP,
  MOBILE_EDGE_CAPTURE,
  REVISED_PROTOTYPE_PLAN,
  REVISED_REVIEW_PLAN,
  NORTHWIND_REVIEW,
] as const satisfies readonly JournalTimelineItem[];

export const JULY11_PROTECTED_ITEM_IDS = [
  RESEARCH_SESSION.itemId,
  PRODUCT_STANDUP.itemId,
  MOBILE_EDGE_CAPTURE.itemId,
] as const;

export const JULY11_FIXED_ITEM_IDS = [NORTHWIND_REVIEW.itemId] as const;

export const JULY11_CAPTURE_TARGETS = [
  {
    targetId: "log",
    label: "Log",
    description: "Record something that happened at a specific time.",
    supportedModes: ["dumb", "smart"],
    defaultMode: "smart",
    enabled: true,
  },
  {
    targetId: "running_notes",
    label: "Running notes",
    description: "Capture an open thought, concern, or idea as a stable Markdown item.",
    supportedModes: ["dumb", "smart"],
    defaultMode: "smart",
    enabled: true,
  },
] as const satisfies readonly JournalCaptureTarget[];

const INITIAL_CAPTURE_SUBMISSIONS = [
  {
    clientMutationId: "capture:july11:0001",
    targetId: "running_notes",
    mode: "smart",
    exactText: "Prototype mobile timeline edge case",
    submittedAt: "2026-07-11T08:55:00-04:00",
    persistenceStatus: "persisted",
    processingStatus: "succeeded",
    annotation: {
      summary: "Captured as a mobile timeline design concern.",
      effects: ["Added to Running notes"],
    },
  },
  {
    clientMutationId: "capture:july11:0002",
    targetId: "log",
    mode: "dumb",
    exactText: "Captured mobile timeline edge case",
    submittedAt: "2026-07-11T11:51:00-04:00",
    persistenceStatus: "persisted",
    processingStatus: "not_requested",
  },
] as const satisfies readonly JournalCaptureSubmission[];

const INITIAL_RUNNING_NOTES = [
  {
    itemId: "running-note:mobile-timeline-edge-case",
    markdown: "Prototype mobile timeline edge case",
    createdAt: "2026-07-11T08:55:00-04:00",
    updatedAt: "2026-07-11T08:55:00-04:00",
    provenance: { source: "user", label: "you" },
    captureMode: "smart",
    processing: {
      state: "succeeded",
      annotation: {
        summary: "Captured as a mobile timeline design concern.",
        effects: ["Added to Running notes"],
      },
    },
    resolutionState: "open",
    version: 1,
  },
] as const satisfies readonly JournalRunningNoteItem[];

export const JULY11_SMART_CAPTURE_INTENT = {
  intent_type: "wb.capture.submit",
  schema_version: 1,
  intent_id: "intent:july11:meeting-ran-long",
  view_id: JOURNAL_VIEW_ID,
  instance_id: JOURNAL_WIDGET_INSTANCE_IDS.capture,
  client_mutation_id: "capture:july11:meeting-ran-long",
  payload: {
    day_id: JULY11_DAY.dayId,
    target_id: "running_notes",
    mode: "smart",
    exact_text: "Meeting ran long",
    stated_at: JULY11_NOW,
  },
} as const satisfies JournalCaptureSubmitIntent;

export const JULY11_DUMB_CAPTURE_INTENT = {
  intent_type: "wb.capture.submit",
  schema_version: 1,
  intent_id: "intent:july11:coffee-refill",
  view_id: JOURNAL_VIEW_ID,
  instance_id: JOURNAL_WIDGET_INSTANCE_IDS.capture,
  client_mutation_id: "capture:july11:coffee-refill",
  payload: {
    day_id: JULY11_DAY.dayId,
    target_id: "log",
    mode: "dumb",
    exact_text: "Coffee refill",
    stated_at: JULY11_NOW,
  },
} as const satisfies JournalCaptureSubmitIntent;

const SMART_ANNOTATION = {
  summary: "The meeting ran long; only the open afternoon was replanned.",
  effects: ["Added to Running notes", "Replanned editable future blocks"],
} as const satisfies CaptureAnnotation;

const SMART_PENDING_SUBMISSION = {
  clientMutationId: JULY11_SMART_CAPTURE_INTENT.client_mutation_id,
  targetId: "running_notes",
  mode: "smart",
  exactText: JULY11_SMART_CAPTURE_INTENT.payload.exact_text,
  submittedAt: JULY11_NOW,
  persistenceStatus: "persisted",
  processingStatus: "pending",
} as const satisfies JournalCaptureSubmission;

const SMART_SETTLED_SUBMISSION = {
  ...SMART_PENDING_SUBMISSION,
  processingStatus: "succeeded",
  annotation: SMART_ANNOTATION,
} as const satisfies JournalCaptureSubmission;

const SMART_PENDING_NOTE = {
  itemId: "running-note:meeting-ran-long",
  markdown: JULY11_SMART_CAPTURE_INTENT.payload.exact_text,
  createdAt: JULY11_NOW,
  updatedAt: JULY11_NOW,
  provenance: { source: "user", label: "you" },
  captureMode: "smart",
  processing: { state: "pending" },
  resolutionState: "open",
  version: 1,
} as const satisfies JournalRunningNoteItem;

const SMART_SETTLED_NOTE = {
  ...SMART_PENDING_NOTE,
  processing: { state: "succeeded", annotation: SMART_ANNOTATION },
  version: 2,
} as const satisfies JournalRunningNoteItem;

const DUMB_SUBMISSION = {
  clientMutationId: JULY11_DUMB_CAPTURE_INTENT.client_mutation_id,
  targetId: "log",
  mode: "dumb",
  exactText: JULY11_DUMB_CAPTURE_INTENT.payload.exact_text,
  submittedAt: JULY11_NOW,
  persistenceStatus: "persisted",
  processingStatus: "not_requested",
} as const satisfies JournalCaptureSubmission;

const DUMB_LOG_ITEM = {
  itemId: "timeline:coffee-refill",
  kind: "record",
  shape: "point",
  at: JULY11_NOW,
  title: JULY11_DUMB_CAPTURE_INTENT.payload.exact_text,
  detail: "you · exact text preserved",
  status: "observed",
  mutability: "past_protected",
  precision: "exact",
  provenance: { source: "user", label: "you" },
  navigation: { targetType: "journal_item", targetId: "log:coffee-refill" },
} as const satisfies JournalTimelineItem;

interface MakeModelOptions {
  readonly fixtureId: string;
  readonly revision: string;
  readonly timelineItems?: readonly JournalTimelineItem[];
  readonly captureSubmissions?: readonly JournalCaptureSubmission[];
  readonly capturesToday?: number;
  readonly runningNotes?: readonly JournalRunningNoteItem[];
}

function makeModel({
  fixtureId,
  revision,
  timelineItems = JULY11_INITIAL_TIMELINE_ITEMS,
  captureSubmissions = INITIAL_CAPTURE_SUBMISSIONS,
  capturesToday = 2,
  runningNotes = INITIAL_RUNNING_NOTES,
}: MakeModelOptions): JournalViewModel {
  const captureInput: JournalCaptureInput = {
    instanceId: JOURNAL_WIDGET_INSTANCE_IDS.capture,
    revision,
    dayId: JULY11_DAY.dayId,
    access: { mode: "read_write" },
    targets: JULY11_CAPTURE_TARGETS,
    capturesToday,
    recentSubmissions: captureSubmissions,
  };

  const timelineInput: JournalTimelineInput = {
    instanceId: JOURNAL_WIDGET_INSTANCE_IDS.timeline,
    revision,
    day: JULY11_DAY,
    access: { mode: "read_write" },
    renderMode: "timeline",
    density: "comfortable",
    items: timelineItems,
  };

  const runningNotesInput: JournalRunningNotesInput = {
    instanceId: JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
    revision,
    dayId: JULY11_DAY.dayId,
    access: { mode: "read_write" },
    displayMode: "chronological",
    items: runningNotes,
  };

  return {
    schemaVersion: 1,
    viewId: JOURNAL_VIEW_ID,
    revision,
    day: JULY11_DAY,
    access: { mode: "read_write" },
    quality: { freshness: "current", observedAt: JULY11_NOW, issues: [] },
    source: { kind: "fixture", fixtureId, label: "Demo data" },
    widgetInputs: {
      [JOURNAL_WIDGET_INSTANCE_IDS.capture]: captureInput,
      [JOURNAL_WIDGET_INSTANCE_IDS.timeline]: timelineInput,
      [JOURNAL_WIDGET_INSTANCE_IDS.runningNotes]: runningNotesInput,
    },
  };
}

export const JULY11_INITIAL_MODEL = makeModel({
  fixtureId: "journal-july11-ready",
  revision: JULY11_INITIAL_REVISION,
});

export const JULY11_SMART_PENDING_MODEL = makeModel({
  fixtureId: "journal-july11-smart-pending",
  revision: JULY11_SMART_PENDING_REVISION,
  captureSubmissions: [...INITIAL_CAPTURE_SUBMISSIONS, SMART_PENDING_SUBMISSION],
  capturesToday: 3,
  runningNotes: [...INITIAL_RUNNING_NOTES, SMART_PENDING_NOTE],
});

export const JULY11_SMART_SETTLED_MODEL = makeModel({
  fixtureId: "journal-july11-smart-settled",
  revision: JULY11_SMART_SETTLED_REVISION,
  timelineItems: JULY11_REVISED_TIMELINE_ITEMS,
  captureSubmissions: [...INITIAL_CAPTURE_SUBMISSIONS, SMART_SETTLED_SUBMISSION],
  capturesToday: 3,
  runningNotes: [...INITIAL_RUNNING_NOTES, SMART_SETTLED_NOTE],
});

export const JULY11_DUMB_PERSISTED_MODEL = makeModel({
  fixtureId: "journal-july11-dumb-persisted",
  revision: JULY11_DUMB_PERSISTED_REVISION,
  timelineItems: [...JULY11_INITIAL_TIMELINE_ITEMS, DUMB_LOG_ITEM],
  captureSubmissions: [...INITIAL_CAPTURE_SUBMISSIONS, DUMB_SUBMISSION],
  capturesToday: 3,
});

export const JULY11_READY_FIXTURE = {
  fixtureId: "journal-july11-ready",
  loadStatus: "ready",
  observedAt: JULY11_NOW,
  model: JULY11_INITIAL_MODEL,
} as const satisfies JournalFixtureState;

export const JULY11_SMART_PENDING_FIXTURE = {
  fixtureId: "journal-july11-smart-pending",
  loadStatus: "ready",
  observedAt: JULY11_NOW,
  model: JULY11_SMART_PENDING_MODEL,
} as const satisfies JournalFixtureState;

export const JULY11_SMART_SETTLED_FIXTURE = {
  fixtureId: "journal-july11-smart-settled",
  loadStatus: "ready",
  observedAt: "2026-07-11T12:18:06-04:00",
  model: JULY11_SMART_SETTLED_MODEL,
} as const satisfies JournalFixtureState;

export const JULY11_DUMB_PERSISTED_FIXTURE = {
  fixtureId: "journal-july11-dumb-persisted",
  loadStatus: "ready",
  observedAt: JULY11_NOW,
  model: JULY11_DUMB_PERSISTED_MODEL,
} as const satisfies JournalFixtureState;

export const JULY11_SMART_CAPTURE_TRANSITION = {
  transitionId: "transition:july11:smart-running-notes",
  fromRevision: JULY11_INITIAL_REVISION,
  intent: JULY11_SMART_CAPTURE_INTENT,
  phases: [
    {
      phase: "accepted",
      snapshot: JULY11_SMART_PENDING_FIXTURE,
      changedInstanceIds: [
        JOURNAL_WIDGET_INSTANCE_IDS.capture,
        JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
      ],
      invariants: [
        "exact_text_preserved",
        "pending_is_provider_owned",
        "cross_widget_change_by_revision",
        "past_items_unchanged",
        "fixed_items_unchanged",
      ],
    },
    {
      phase: "settled",
      snapshot: JULY11_SMART_SETTLED_FIXTURE,
      changedInstanceIds: [
        JOURNAL_WIDGET_INSTANCE_IDS.capture,
        JOURNAL_WIDGET_INSTANCE_IDS.timeline,
        JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
      ],
      invariants: [
        "exact_text_preserved",
        "smart_annotations_do_not_rewrite",
        "cross_widget_change_by_revision",
        "past_items_unchanged",
        "fixed_items_unchanged",
      ],
    },
  ],
} as const satisfies JournalExpectedProviderTransition;

export const JULY11_DUMB_CAPTURE_TRANSITION = {
  transitionId: "transition:july11:dumb-log",
  fromRevision: JULY11_INITIAL_REVISION,
  intent: JULY11_DUMB_CAPTURE_INTENT,
  phases: [
    {
      phase: "accepted",
      snapshot: JULY11_DUMB_PERSISTED_FIXTURE,
      changedInstanceIds: [
        JOURNAL_WIDGET_INSTANCE_IDS.capture,
        JOURNAL_WIDGET_INSTANCE_IDS.timeline,
      ],
      invariants: [
        "exact_text_preserved",
        "no_per_entry_compute",
        "cross_widget_change_by_revision",
        "past_items_unchanged",
        "fixed_items_unchanged",
      ],
    },
  ],
} as const satisfies JournalExpectedProviderTransition;
