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

/** Stable provider-owned instance identity; the three constants are legacy/default aliases. */
export type JournalWidgetInstanceId = string;

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
  | "local_submission"
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
  readonly instanceId: JournalWidgetInstanceId;
  readonly revision: JournalRevision;
  readonly day: JournalDayBinding;
  readonly access: JournalAccess;
  readonly accessNotice?: "widget" | "view";
  readonly renderMode: TimelineRenderMode;
  readonly density: TimelineDensity;
  readonly items: readonly JournalTimelineItem[];
}

/** A destination from the effective day schema, never a client-side closed catalog. */
export type JournalCaptureTargetId = string;
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
export type CaptureProcessingStatus =
  | "not_requested"
  | "pending"
  | "running"
  | "succeeded"
  | "failed";
export type CapturePlacementStatus = "pending" | "placed" | "failed";

export interface CaptureAnnotation {
  readonly summary: string;
  readonly effects: readonly string[];
}

export interface JournalCaptureSubmission {
  readonly captureId?: string;
  readonly clientMutationId: string;
  readonly targetId: JournalCaptureTargetId;
  readonly mode: JournalCaptureMode;
  /** Exact user input. Providers and renderers must not trim, normalize, or rewrite it. */
  readonly exactText?: string;
  readonly submittedAt: IsoDateTime;
  readonly persistenceStatus: CapturePersistenceStatus;
  readonly placementStatus?: CapturePlacementStatus;
  /** Dumb captures always use `not_requested`; they never enter per-entry processing. */
  readonly processingStatus: CaptureProcessingStatus;
  readonly annotation?: CaptureAnnotation;
  readonly errorMessage?: string;
  readonly sourceRef?: string;
  readonly revision?: number;
  readonly retryable?: boolean;
  readonly followUps?: readonly CaptureFollowUp[];
}

export interface JournalCaptureInput {
  readonly instanceId: JournalWidgetInstanceId;
  readonly revision: JournalRevision;
  readonly dayId: string;
  readonly access: JournalAccess;
  readonly accessNotice?: "widget" | "view";
  readonly smartAvailability?: CaptureSmartAvailability;
  readonly secondaryActions?: readonly CaptureSecondaryAction[];
  readonly smartHelp?: {
    readonly summary: string;
    readonly details: string;
  };
  readonly targets: readonly JournalCaptureTarget[];
  readonly capturesToday: number;
  readonly recentSubmissions: readonly JournalCaptureSubmission[];
}

export type RunningNoteProcessingState =
  | "not_requested"
  | "pending"
  | "running"
  | "succeeded"
  | "failed";
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
  readonly threadId?: string;
  readonly version: number;
  /** Native composition ownership; absent only for retained legacy entries. */
  readonly moduleInstanceId?: JournalWidgetInstanceId;
  readonly moduleInstanceVersion?: number;
  readonly followUps?: readonly CaptureFollowUp[];
  readonly document?:
    | {
        readonly state: "available";
        readonly gestureContextSha256: string;
      }
    | {
        readonly state: "current" | "paused_diverged";
        readonly gestureContextSha256: string;
        readonly href: string;
        readonly storeId: string;
        readonly documentId: string;
        readonly changeId: string;
        readonly contentAuthorityEpoch: number;
      };
}

/**
 * Provider/App-owned deletion record. Tombstones never need to be sent to the widget
 * renderer, but the Journal store must retain them so history and diff context remain
 * honest after an item disappears from the active collection.
 */
export interface JournalRunningNoteTombstone {
  readonly item: JournalRunningNoteItem;
  readonly deletedAt: IsoDateTime;
  readonly deletedVersion: number;
  readonly deletedBy: TimelineProvenance;
  readonly reason: "user_deleted";
}

export type RunningNotesDisplayMode = "chronological" | "grouped";

export interface JournalRunningNotesInput {
  readonly instanceId: JournalWidgetInstanceId;
  readonly revision: JournalRevision;
  readonly dayId: string;
  readonly access: JournalAccess;
  readonly displayMode: RunningNotesDisplayMode;
  readonly items: readonly JournalRunningNoteItem[];
  readonly supplementalItems?: readonly JournalNativeItemInput[];
  readonly tombstones?: readonly JournalRunningNoteItem[];
}

export type JournalFieldValueKind =
  | "short_text"
  | "long_text"
  | "number"
  | "scale"
  | "boolean"
  | "single_select"
  | "multi_select"
  | "local_time"
  | "instant"
  | "date"
  | "duration"
  | "reference";

export type JournalFieldValue =
  | string
  | number
  | boolean
  | readonly string[]
  | readonly JournalFieldReference[]
  | null;

export interface JournalFieldReference {
  readonly kind: string;
  readonly id: string;
  readonly revision?: string;
}

export interface JournalFieldOption {
  readonly value: string;
  readonly label: string;
}

export interface JournalEffectiveFieldInput {
  readonly valueId?: string;
  readonly compositionSlotId: string;
  readonly fieldId: string;
  readonly definitionVersion: number;
  readonly promptId?: string;
  readonly promptVersion?: number;
  readonly label: string;
  readonly description?: string;
  readonly valueKind: JournalFieldValueKind;
  readonly value: JournalFieldValue;
  readonly valueRevision?: number;
  readonly required: boolean;
  readonly unit?: string;
  readonly functionId?: string;
  readonly functionVersion?: number;
  readonly minimum?: number;
  readonly maximum?: number;
  readonly options?: readonly JournalFieldOption[];
  readonly disposition?: "missing" | "skipped" | "declined";
  readonly readOnly?: boolean;
  readonly unavailableReason?: string;
  readonly authorship?: string;
  readonly reviewState?: string;
  readonly sourceRef?: string;
}

export interface JournalDocumentModuleAvailable {
  readonly state: "available";
  readonly role: string;
  readonly truthEligibility: "allowed";
  readonly truthStartsDisabled: true;
}

export interface JournalDocumentModuleCurrent {
  readonly state: "current";
  readonly role: string;
  readonly truthEligibility: "allowed";
  readonly truthStartsDisabled: true;
  readonly href: string;
  readonly storeId: string;
  readonly documentId: string;
  readonly bindingId: string;
  readonly domainEntityId: string;
  readonly contentAuthorityEpoch: number;
  readonly canOpenFull: boolean;
}

export type JournalDocumentModuleState =
  | JournalDocumentModuleAvailable
  | JournalDocumentModuleCurrent;

/** Safe generic input for data-defined Journal module instances. */
export interface JournalGenericModuleInput {
  readonly instanceId: JournalWidgetInstanceId;
  readonly revision: JournalRevision;
  readonly dayId: string;
  readonly localDate: string;
  readonly access: JournalAccess;
  readonly moduleTypeId: string;
  readonly moduleInstanceVersion: number;
  readonly moduleDefinitionVersion: number;
  readonly behaviorId: string | null;
  readonly behaviorVersion: number | null;
  readonly aiContribution: "forbidden" | "allowed" | "suggestion_only";
  readonly label: string;
  readonly description?: string;
  readonly fields: readonly JournalEffectiveFieldInput[];
  readonly items?: readonly JournalNativeItemInput[];
  readonly promptInteractions?: readonly JournalPromptInteraction[];
  readonly document?: JournalDocumentModuleState;
  readonly unavailableReason?: string;
}

export interface JournalNativeItemInput {
  readonly itemId: string;
  readonly itemKind: string;
  readonly text: string;
  readonly createdAt: IsoDateTime;
  readonly updatedAt: IsoDateTime;
  readonly revision: number;
  readonly lifecycle: string;
  readonly authorityKind: string;
  readonly sourceRef?: string;
  readonly actions: readonly JournalItemAction[];
  readonly relations: readonly JournalItemRelation[];
}

export type JournalItemAction =
  | "edit"
  | "correct"
  | "resolve"
  | "route"
  | "tombstone"
  | "restore";

export interface JournalItemRelation {
  readonly relationId: string;
  readonly relationKind: string;
  readonly targetDomain: string;
  readonly targetId: string;
  readonly targetRevision?: string;
  readonly lifecycle: string;
  readonly revision: number;
}

export interface JournalPromptGenerationRequest {
  readonly requestId: string;
  readonly status: "pending" | "leased" | "succeeded" | "failed" | "canceled" | "expired";
  readonly retryable: boolean;
  readonly attempts: number;
  readonly providerId?: string;
  readonly modelId?: string;
  readonly errorCode?: string;
  readonly createdAt: IsoDateTime;
  readonly updatedAt: IsoDateTime;
  readonly completedAt?: IsoDateTime;
}

export interface JournalPromptResultVariant {
  readonly variantId: string;
  readonly resultText: string;
  readonly sourceRef?: string;
  readonly authorship: string;
  readonly reviewState: string;
  readonly lifecycle: string;
  readonly producerId: string;
  readonly providerId?: string;
  readonly modelId?: string;
  readonly createdAt: IsoDateTime;
}

export interface JournalPromptInteraction {
  readonly interactionId: string;
  readonly moduleInstanceId: string;
  readonly moduleInstanceVersion: number;
  readonly promptId: string;
  readonly promptVersion: number;
  readonly promptWording: string;
  readonly promptHelp?: string;
  readonly inputText: string;
  readonly inputSourceRef?: string;
  readonly lifecycle: string;
  readonly currentRevision: number;
  readonly variants: readonly JournalPromptResultVariant[];
  readonly generationRequests: readonly JournalPromptGenerationRequest[];
}

export interface JournalEffectiveModule {
  readonly slotId: string;
  readonly ordinal: number;
  readonly moduleInstanceId: JournalWidgetInstanceId;
  readonly moduleInstanceVersion: number;
  readonly moduleTypeId: string;
  readonly moduleTypeVersion: number;
  readonly label: string;
  readonly behaviorId: string | null;
  readonly behaviorVersion: number | null;
  readonly aiContribution: "forbidden" | "allowed" | "suggestion_only";
  readonly semanticMembership: "included" | "excluded_by_schedule" | "unavailable";
  readonly settings: Readonly<Record<string, unknown>>;
  readonly scheduleKind: string;
  readonly scheduleEvidence: unknown;
  readonly document?: JournalDocumentModuleState;
  readonly fields: readonly {
    readonly compositionSlotId: string;
    readonly ordinal: number;
    readonly fieldId: string;
    readonly fieldDefinitionVersion: number;
    /** Definition metadata is carried by the immutable day composition. */
    readonly label?: string;
    readonly description?: string;
    readonly valueKind?: JournalFieldValueKind;
    readonly unit?: string | null;
    readonly functionId?: string | null;
    readonly functionVersion?: number | null;
    readonly constraints?: Readonly<Record<string, unknown>>;
    readonly behaviorId?: string;
    readonly behaviorVersion?: number;
    readonly privacyClass?: string;
    readonly searchMode?: string;
    readonly promptId: string | null;
    readonly promptVersion: number | null;
    readonly promptWording?: string | null;
    readonly promptHelp?: string | null;
    readonly promptRequiredness?: string | null;
  }[];
}

export interface JournalEffectiveComposition {
  readonly schemaVersion: 1;
  readonly persisted: boolean;
  readonly snapshotId: string | null;
  readonly snapshotVersion: number | null;
  readonly compositionDigest: string;
  readonly searchRecipeVersion: number;
  readonly activationRevision: number;
  readonly authorityState:
    | "legacy_compatibility"
    | "database_only"
    | "recovery_fenced";
  readonly profile: {
    readonly profileId: string;
    readonly profileRevision: number;
    readonly formatVersion: number;
    readonly name: string;
    readonly description: string;
    readonly profileDigest: string;
  };
  readonly modules: readonly JournalEffectiveModule[];
}

export type JournalWidgetInput =
  | JournalCaptureInput
  | JournalTimelineInput
  | JournalRunningNotesInput
  | JournalGenericModuleInput;

export interface JournalWidgetInputs {
  readonly [instanceId: string]: JournalWidgetInput;
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
  /** Frozen server/domain composition for this logical day, when native authority supplies it. */
  readonly effectiveComposition?: JournalEffectiveComposition;
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
    "wb.capture.submit",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.capture
  > {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly day_id: string;
    readonly target_id: JournalCaptureTargetId;
    readonly mode: JournalCaptureMode;
    readonly exact_text: string;
    readonly stated_at?: IsoDateTime;
    readonly follow_up_action?: string;
    readonly smart_disclosure_sha256?: string;
  };
}

export interface JournalTimelineOpenItemIntent
  extends JournalIntentEnvelope<
    "wb.timeline.open-item",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.timeline
  > {
  readonly payload: { readonly item_id: string };
}

export interface JournalTimelineItemActionIntent
  extends JournalIntentEnvelope<
    "wb.timeline.item-action-requested",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.timeline
  > {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly item_id: string;
    readonly action_id: string;
    readonly expected_revision: JournalRevision;
  };
}

export interface JournalTimelineRequestReplanIntent
  extends JournalIntentEnvelope<
    "wb.timeline.replan-requested",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.timeline
  > {
  readonly payload: { readonly day_id: string; readonly preserve_before: IsoDateTime };
}

export interface JournalTimelineSetRenderModeIntent
  extends JournalIntentEnvelope<
    "wb.timeline.render-mode-changed",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.timeline
  > {
  readonly payload: { readonly render_mode: TimelineRenderMode };
}

export interface JournalRunningNoteEditIntent
  extends JournalIntentEnvelope<
    "wb.notes.edit-requested",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
  > {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly item_id: string;
    readonly expected_version: number;
    readonly markdown: string;
  };
}

export interface JournalRunningNoteDeleteIntent
  extends JournalIntentEnvelope<
    "wb.notes.delete-requested",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
  > {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly item_id: string;
    readonly expected_version: number;
  };
}

export interface JournalRunningNoteOpenThreadIntent
  extends JournalIntentEnvelope<
    "wb.notes.open-thread-requested",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
  > {
  readonly payload: { readonly item_id: string; readonly thread_id: string };
}

export interface JournalRunningNoteOpenDocumentIntent
  extends JournalIntentEnvelope<
    "wb.notes.open-document-requested",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
  > {
  readonly payload: {
    readonly item_id: string;
    readonly expected_version: number;
    readonly gesture_context_sha256: string;
  };
}

export interface JournalRunningNoteRestoreIntent
  extends JournalIntentEnvelope<
    "wb.notes.restore-requested",
    typeof JOURNAL_WIDGET_INSTANCE_IDS.runningNotes
  > {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly item_id: string;
    readonly expected_version: number;
  };
}

export interface JournalFieldValuePutIntent
  extends JournalIntentEnvelope<"wb.journal.field-value.put", JournalWidgetInstanceId> {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly local_date: string;
    readonly module_instance_id: string;
    readonly module_instance_version: number;
    readonly composition_slot_id: string;
    readonly field_id: string;
    readonly field_definition_version: number;
    readonly value_id?: string;
    readonly expected_revision: number;
    readonly value: JournalFieldValue;
    readonly disposition?: "missing" | "skipped" | "declined";
    readonly exact_input: string;
    readonly stated_at?: IsoDateTime;
  };
}

export interface JournalItemActionIntent
  extends JournalIntentEnvelope<"wb.journal.item-action", JournalWidgetInstanceId> {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly item_id: string;
    readonly action: JournalItemAction;
    readonly expected_revision: number;
    readonly exact_text?: string;
    readonly target_domain?: string;
    readonly target_id?: string;
    readonly target_revision?: string;
  };
}

export interface JournalPromptCreateIntent
  extends JournalIntentEnvelope<"wb.journal.prompt-create", JournalWidgetInstanceId> {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly local_date: string;
    readonly module_instance_id: string;
    readonly module_instance_version: number;
    readonly prompt_id: string;
    readonly prompt_version: number;
    readonly exact_input: string;
  };
}

export interface JournalPromptGenerateIntent
  extends JournalIntentEnvelope<"wb.journal.prompt-generate", JournalWidgetInstanceId> {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly interaction_id: string;
    readonly expected_revision: number;
  };
}

export interface JournalPromptDecisionIntent
  extends JournalIntentEnvelope<"wb.journal.prompt-decision", JournalWidgetInstanceId> {
  readonly client_mutation_id: string;
  readonly payload: {
    readonly interaction_id: string;
    readonly variant_id: string;
    readonly expected_revision: number;
    readonly decision: "accept" | "archive" | "reject";
  };
}

export type JournalIntent =
  | JournalCaptureSubmitIntent
  | JournalTimelineOpenItemIntent
  | JournalTimelineItemActionIntent
  | JournalTimelineRequestReplanIntent
  | JournalTimelineSetRenderModeIntent
  | JournalRunningNoteEditIntent
  | JournalRunningNoteDeleteIntent
  | JournalRunningNoteRestoreIntent
  | JournalRunningNoteOpenThreadIntent
  | JournalRunningNoteOpenDocumentIntent
  | JournalFieldValuePutIntent
  | JournalItemActionIntent
  | JournalPromptCreateIntent
  | JournalPromptGenerateIntent
  | JournalPromptDecisionIntent;

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
  | "smart_annotations_do_not_rewrite"
  | "deleted_items_tombstoned";

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
import type { CaptureFollowUp, CaptureSecondaryAction, CaptureSmartAvailability } from "../../widget-library/capture/contracts";
