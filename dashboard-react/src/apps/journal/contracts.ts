/**
 * Journal-owned presentation contracts for the dashboard scaffold.
 *
 * These types deliberately describe already-bound UI data and UI intent. They do not
 * expose endpoints, capabilities, persistence, SSE, or planner implementations. A
 * Journal ViewProvider owns every cross-widget transition and publishes a new revision.
 */

export const JOURNAL_VIEW_ID = "wb.journal.main" as const;

export const JOURNAL_WIDGET_INSTANCE_IDS = {
  capture: "default:capture",
  timeline: "default:timeline",
  runningNotes: "default:running-notes",
} as const;

export type JournalWidgetInstanceId =
  (typeof JOURNAL_WIDGET_INSTANCE_IDS)[keyof typeof JOURNAL_WIDGET_INSTANCE_IDS];

export type IsoDate = string;
export type IsoDateTime = string;
export type LocalTime = string;
export type JournalRevision = string;

/**
 * The selected journal day is view context, not widget-owned state.
 *
 * `dayBoundaryStart` defines which local-day window owns an instant. `openedAt` is the
 * actual first-touch/open timestamp and must never be inferred from the boundary.
 */
export interface JournalDayBinding {
  readonly dayId: string;
  readonly localDate: IsoDate;
  readonly timezone: string;
  readonly dayBoundaryStart: LocalTime;
  readonly windowStart: IsoDateTime;
  readonly windowEnd: IsoDateTime;
  readonly openedAt?: IsoDateTime;
  readonly closedAt?: IsoDateTime;
  readonly now: IsoDateTime;
}

export type JournalAccess =
  | { readonly mode: "read_write" }
  | { readonly mode: "read_only"; readonly reason: string };

export type JournalFreshness = "current" | "stale" | "offline";

export interface JournalDataIssue {
  readonly code: string;
  readonly message: string;
  readonly affectedInstanceIds: readonly JournalWidgetInstanceId[];
}

export interface JournalDataQuality {
  readonly freshness: JournalFreshness;
  readonly observedAt: IsoDateTime;
  readonly issues: readonly JournalDataIssue[];
}

export type JournalDemoSource =
  | { readonly kind: "fixture"; readonly fixtureId: string; readonly label: "Demo data" }
  | { readonly kind: "in_memory"; readonly fixtureId: string; readonly label: "Demo data" }
  | { readonly kind: "live" };

export type TimelineItemKind = "record" | "calendar" | "plan";
export type TimelineItemShape = "point" | "span";
export type TimelineItemStatus = "observed" | "planned" | "completed" | "cancelled";
export type TimelineItemMutability = "past_protected" | "fixed" | "editable";
export type TimelinePrecision = "exact" | "derived" | "approximate";
export type TimelineProvenanceSource =
  | "user"
  | "agent"
  | "calendar"
  | "conversation_observability"
  | "planner";

export interface TimelineProvenance {
  readonly source: TimelineProvenanceSource;
  readonly label: string;
  readonly actor?: string;
}

export type TimelineTemporalPlacement =
  | {
      readonly shape: "point";
      readonly at: IsoDateTime;
    }
  | {
      readonly shape: "span";
      readonly startAt: IsoDateTime;
      readonly endAt: IsoDateTime;
    };

export interface TimelineNavigationTarget {
  readonly targetType: "calendar_event" | "journal_item" | "session" | "task" | "thread";
  readonly targetId: string;
}

export type JournalTimelineItem = TimelineTemporalPlacement & {
  readonly itemId: string;
  readonly kind: TimelineItemKind;
  readonly title: string;
  readonly detail?: string;
  readonly status: TimelineItemStatus;
  readonly mutability: TimelineItemMutability;
  readonly precision: TimelinePrecision;
  readonly provenance: TimelineProvenance;
  readonly navigation?: TimelineNavigationTarget;
};

export type TimelineRenderMode = "timeline" | "list";
export type TimelineDensity = "comfortable" | "compact";

export interface JournalTimelineInput {
  readonly instanceId: typeof JOURNAL_WIDGET_INSTANCE_IDS.timeline;
  readonly revision: JournalRevision;
  readonly day: JournalDayBinding;
  readonly renderMode: TimelineRenderMode;
  readonly density: TimelineDensity;
  readonly items: readonly JournalTimelineItem[];
}

export type JournalCaptureTargetId = "log" | "running_notes";
export type JournalCaptureMode = "dumb" | "smart";

export interface JournalCaptureTarget {
  readonly targetId: JournalCaptureTargetId;
  readonly label: string;
  readonly description: string;
  readonly supportedModes: readonly JournalCaptureMode[];
  readonly defaultMode: JournalCaptureMode;
  readonly enabled: boolean;
  readonly unavailableReason?: string;
}

export type CapturePersistenceStatus = "persisted" | "failed";
export type CaptureProcessingStatus = "not_requested" | "pending" | "succeeded" | "failed";

export interface CaptureAnnotation {
  readonly summary: string;
  readonly effects: readonly string[];
}

export interface JournalCaptureSubmission {
  readonly clientMutationId: string;
  readonly targetId: JournalCaptureTargetId;
  readonly mode: JournalCaptureMode;
  /** Exact user input. Providers and renderers must not trim, normalize, or rewrite it. */
  readonly exactText: string;
  readonly submittedAt: IsoDateTime;
  readonly persistenceStatus: CapturePersistenceStatus;
  /** Dumb captures always use `not_requested`; they never enter per-entry processing. */
  readonly processingStatus: CaptureProcessingStatus;
  readonly annotation?: CaptureAnnotation;
  readonly errorMessage?: string;
}

export interface JournalCaptureInput {
  readonly instanceId: typeof JOURNAL_WIDGET_INSTANCE_IDS.capture;
  readonly revision: JournalRevision;
  readonly dayId: string;
  readonly access: JournalAccess;
  readonly targets: readonly JournalCaptureTarget[];
  readonly capturesToday: number;
  readonly recentSubmissions: readonly JournalCaptureSubmission[];
}

export type RunningNoteProcessingState = "not_requested" | "pending" | "succeeded" | "failed";
export type RunningNoteResolutionState =
  | "open"
  | "routed_to_task"
  | "routed_to_consideration"
  | "appended"
  | "dismissed";

export interface RunningNoteProcessing {
  readonly state: RunningNoteProcessingState;
  readonly annotation?: CaptureAnnotation;
  readonly errorMessage?: string;
}

export interface JournalRunningNoteItem {
  readonly itemId: string;
  /** Markdown is the item's content format; the item identity is stable independently. */
  readonly markdown: string;
  readonly createdAt: IsoDateTime;
  readonly updatedAt: IsoDateTime;
  readonly provenance: TimelineProvenance;
  readonly captureMode: JournalCaptureMode;
  readonly processing: RunningNoteProcessing;
  readonly resolutionState: RunningNoteResolutionState;
  readonly groupId?: string;
  readonly version: number;
}

export type RunningNotesDisplayMode = "chronological" | "grouped";

export interface JournalRunningNotesInput {
  readonly instanceId: typeof JOURNAL_WIDGET_INSTANCE_IDS.runningNotes;
  readonly revision: JournalRevision;
  readonly dayId: string;
  readonly access: JournalAccess;
  readonly displayMode: RunningNotesDisplayMode;
  readonly items: readonly JournalRunningNoteItem[];
}

export interface JournalWidgetInputs {
  readonly [JOURNAL_WIDGET_INSTANCE_IDS.capture]: JournalCaptureInput;
  readonly [JOURNAL_WIDGET_INSTANCE_IDS.timeline]: JournalTimelineInput;
  readonly [JOURNAL_WIDGET_INSTANCE_IDS.runningNotes]: JournalRunningNotesInput;
}

export interface JournalViewModel {
  readonly schemaVersion: 1;
  readonly viewId: typeof JOURNAL_VIEW_ID;
  readonly revision: JournalRevision;
  readonly day: JournalDayBinding;
  readonly access: JournalAccess;
  readonly quality: JournalDataQuality;
  readonly source: JournalDemoSource;
  readonly widgetInputs: JournalWidgetInputs;
}

interface JournalIntentEnvelope<IntentType extends string, InstanceId extends JournalWidgetInstanceId> {
  readonly intent_type: IntentType;
  readonly schema_version: 1;
  readonly intent_id: string;
  readonly view_id: typeof JOURNAL_VIEW_ID;
  readonly instance_id: InstanceId;
}

export interface JournalCaptureSubmitIntent
  extends JournalIntentEnvelope<
    "wb.journal.capture.submit",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.capture
  > {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly day_id: string;
    readonly target_id: JournalCaptureTargetId;
    readonly mode: JournalCaptureMode;
    readonly exact_text: string;
    readonly stated_at?: IsoDateTime;
  };
}

export interface JournalTimelineOpenItemIntent
  extends JournalIntentEnvelope<
    "wb.journal.timeline.open-item",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.timeline
  > {
  readonly payload: { readonly item_id: string };
}

export interface JournalTimelineRequestReplanIntent
  extends JournalIntentEnvelope<
    "wb.journal.timeline.replan-requested",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.timeline
  > {
  readonly payload: { readonly day_id: string; readonly preserve_before: IsoDateTime };
}

export interface JournalTimelineSetRenderModeIntent
  extends JournalIntentEnvelope<
    "wb.journal.timeline.render-mode-changed",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.timeline
  > {
  readonly payload: { readonly render_mode: TimelineRenderMode };
}

export interface JournalRunningNoteEditIntent
  extends JournalIntentEnvelope<
    "wb.journal.running-notes.edit-requested",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
  > {
  readonly payload: {
    readonly item_id: string;
    readonly expected_version: number;
    readonly markdown: string;
  };
}

export interface JournalRunningNoteOpenThreadIntent
  extends JournalIntentEnvelope<
    "wb.journal.running-notes.open-thread-requested",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
  > {
  readonly payload: { readonly item_id: string; readonly thread_id: string };
}

export type JournalIntent =
  | JournalCaptureSubmitIntent
  | JournalTimelineOpenItemIntent
  | JournalTimelineRequestReplanIntent
  | JournalTimelineSetRenderModeIntent
  | JournalRunningNoteEditIntent
  | JournalRunningNoteOpenThreadIntent;

export type JournalFixtureLoadStatus = "loading" | "ready" | "stale" | "offline" | "error";

export type JournalFixtureState =
  | {
      readonly fixtureId: string;
      readonly loadStatus: "loading";
      readonly observedAt: IsoDateTime;
      readonly model: null;
    }
  | {
      readonly fixtureId: string;
      readonly loadStatus: "ready" | "stale" | "offline";
      readonly observedAt: IsoDateTime;
      readonly model: JournalViewModel;
    }
  | {
      readonly fixtureId: string;
      readonly loadStatus: "error";
      readonly observedAt: IsoDateTime;
      readonly model: null;
      readonly error: { readonly code: string; readonly message: string; readonly retryable: boolean };
    };

export type JournalTransitionInvariant =
  | "exact_text_preserved"
  | "no_per_entry_compute"
  | "pending_is_provider_owned"
  | "cross_widget_change_by_revision"
  | "past_items_unchanged"
  | "fixed_items_unchanged"
  | "smart_annotations_do_not_rewrite";

export interface JournalExpectedTransitionPhase {
  readonly phase: "accepted" | "settled";
  readonly snapshot: JournalFixtureState & { readonly model: JournalViewModel };
  readonly changedInstanceIds: readonly JournalWidgetInstanceId[];
  readonly invariants: readonly JournalTransitionInvariant[];
}

export interface JournalExpectedProviderTransition {
  readonly transitionId: string;
  readonly fromRevision: JournalRevision;
  readonly intent: JournalIntent;
  readonly phases: readonly JournalExpectedTransitionPhase[];
}
