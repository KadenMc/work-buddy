import type {
  AppInvalidation,
  DashboardIntent,
  IntentResult,
  ReconcileResult,
  SnapshotStatus,
  ViewId,
  ViewLoadRequest,
  ViewSnapshot,
  WidgetLoadRequest,
  WidgetSnapshot,
  WidgetTypeId,
} from "../../../dashboard/contributions/contracts";
import type { ViewProvider } from "../../../dashboard/providers/ViewProvider";
import type {
  DayTimelineInput,
  DayTimelineItem,
  TimelineDayWindow,
} from "../../../widget-library/timeline/contracts";
import {
  JOURNAL_APP_ID,
  JOURNAL_BINDING_KEYS,
  JOURNAL_INSTANCE_IDS,
  JOURNAL_VIEW_DEFINITION_ID,
  JOURNAL_WIDGET_TYPE_BY_INSTANCE,
  JOURNAL_WIDGET_TYPE_IDS,
} from "../bindings";
import {
  JOURNAL_VIEW_ID,
  JOURNAL_WIDGET_INSTANCE_IDS,
  type JournalAccess,
  type JournalDataQuality,
  type JournalDayBinding,
  type JournalDemoSource,
  type JournalWidgetInstanceId,
} from "../contracts";

export const LEGACY_TODAY_ENDPOINT = "/api/automation/today" as const;

export type LegacyTodaySourceStatus = "ok" | "degraded" | "error";

export interface LegacyTodayNow {
  readonly iso: string;
  readonly local_hhmm: string;
  readonly minutes_into_day: number;
}

export interface LegacyTodayPlanItem {
  readonly time_start: string;
  readonly time_end: string;
  readonly text: string;
  readonly checked: boolean;
}

export interface LegacyTodayPayload {
  readonly status: LegacyTodaySourceStatus;
  readonly now: LegacyTodayNow;
  readonly work_hours: readonly [number, number];
  readonly current_contexts: readonly string[];
  readonly recommendations: readonly unknown[];
  readonly plan: readonly LegacyTodayPlanItem[];
  readonly plan_status?: unknown;
  readonly focused_count: number;
  readonly calendar_event_count: number;
  readonly active_contracts: readonly unknown[];
  readonly contract_constraints: readonly unknown[];
  readonly engage_count: number;
  readonly errors: readonly string[];
}

export interface LegacyAdapterLimitation {
  readonly capability: string;
  readonly state: "unavailable";
  readonly reason: string;
}

/**
 * Deliberate non-mappings. The legacy Today response is never promoted into native
 * Journal data merely because a renderer could display a similar-looking value.
 */
export const LEGACY_JOURNAL_UNSUPPORTED = [
  {
    capability: "native_day_container",
    state: "unavailable",
    reason: "The Today response does not expose a native Journal day or day boundary.",
  },
  {
    capability: "capture_persistence",
    state: "unavailable",
    reason: "The Today endpoint is read-only and exposes no capture persistence contract.",
  },
  {
    capability: "running_notes_records",
    state: "unavailable",
    reason: "The Today response does not expose native Running Notes records.",
  },
  {
    capability: "observed_log_records",
    state: "unavailable",
    reason: "The Today response does not expose observed Journal log records.",
  },
  {
    capability: "calendar_provenance",
    state: "unavailable",
    reason: "A calendar count is not enough to reconstruct calendar-backed items or provenance.",
  },
  {
    capability: "smart_processing",
    state: "unavailable",
    reason: "The Today response exposes no smart-write lifecycle or annotations.",
  },
  {
    capability: "write_intents",
    state: "unavailable",
    reason: "No Dashboard write intent is mapped to this read-only endpoint.",
  },
  {
    capability: "native_revision",
    state: "unavailable",
    reason: "The endpoint has no revision; the adapter revision is only a projection hash.",
  },
] as const satisfies readonly LegacyAdapterLimitation[];

export interface LegacyFieldMapping {
  readonly source: string;
  readonly target: string;
  readonly fidelity: "direct" | "derived" | "metadata_only";
  readonly note: string;
}

export const LEGACY_TODAY_FIELD_MAPPING = [
  {
    source: "now",
    target: "Journal day/timeline now",
    fidelity: "direct",
    note: "The endpoint timestamp is preserved; local minute metadata anchors same-day derivation.",
  },
  {
    source: "work_hours",
    target: "Timeline window",
    fidelity: "derived",
    note: "Work-hour bounds define only the visible window, never the Journal day boundary.",
  },
  {
    source: "plan",
    target: "Timeline plan items",
    fidelity: "derived",
    note: "Generated plan rows remain plan items, including rows whose text starts with [Cal].",
  },
  {
    source: "recommendations",
    target: "Legacy view metadata",
    fidelity: "metadata_only",
    note: "Recommendations have no temporal placement and are not invented as timeline items.",
  },
  {
    source: "status/errors",
    target: "Snapshot quality and legacy source state",
    fidelity: "direct",
    note: "Degraded and error states remain visible rather than being normalized to ready.",
  },
  {
    source: "calendar_event_count",
    target: "Legacy view metadata",
    fidelity: "metadata_only",
    note: "The count is retained, but no calendar records or provenance are reconstructed.",
  },
] as const satisfies readonly LegacyFieldMapping[];

export interface LegacyJournalMetadata {
  readonly endpoint: typeof LEGACY_TODAY_ENDPOINT;
  readonly sourceStatus: LegacyTodaySourceStatus;
  readonly currentContexts: readonly string[];
  readonly recommendations: readonly unknown[];
  readonly planStatus?: unknown;
  readonly focusedCount: number;
  readonly calendarEventCount: number;
  readonly activeContracts: readonly unknown[];
  readonly contractConstraints: readonly unknown[];
  readonly engageCount: number;
  readonly errors: readonly string[];
  readonly timelineWindowSource: "legacy_work_hours";
  readonly revisionSource: "adapter_projection_hash";
  readonly limitations: readonly LegacyAdapterLimitation[];
}

/**
 * A live but partial Journal-shaped view model. Its chrome fields intentionally match
 * the demo model, while widget inputs contain only data the Today endpoint truly owns.
 */
export interface LegacyJournalViewModel {
  readonly schemaVersion: 1;
  readonly viewId: typeof JOURNAL_VIEW_ID;
  readonly revision: string;
  readonly day: JournalDayBinding;
  readonly access: JournalAccess;
  readonly quality: JournalDataQuality;
  readonly source: Extract<JournalDemoSource, { readonly kind: "live" }>;
  readonly widgetInputs: Readonly<Partial<Record<JournalWidgetInstanceId, DayTimelineInput>>>;
  readonly legacy: LegacyJournalMetadata;
}

export type LegacyJournalBindingValue =
  | JournalDayBinding
  | JournalAccess
  | JournalDataQuality
  | LegacyJournalViewModel["source"];

export type LegacyJournalViewSnapshot = ViewSnapshot<
  LegacyJournalViewModel | null,
  LegacyJournalBindingValue,
  DayTimelineInput
>;

export type LegacyJournalWidgetSnapshot = WidgetSnapshot<DayTimelineInput | null>;

export interface LegacyFlaskViewAdapterOptions {
  readonly fetchImpl?: typeof fetch;
  readonly timezone?: string;
  readonly clock?: () => string;
}

const READ_ONLY_REASON =
  "Legacy Today data is a partial read-only projection; Journal write behavior is unavailable.";

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function asArray(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

function asStringArray(value: unknown): readonly string[] {
  return asArray(value).filter((item): item is string => typeof item === "string");
}

function normalizeStatus(value: unknown): LegacyTodaySourceStatus {
  if (value === "ok" || value === "degraded" || value === "error") return value;
  return "degraded";
}

function parsePlan(value: unknown): readonly LegacyTodayPlanItem[] {
  return asArray(value).flatMap((candidate) => {
    if (
      !isRecord(candidate) ||
      typeof candidate.time_start !== "string" ||
      typeof candidate.time_end !== "string" ||
      typeof candidate.text !== "string"
    ) {
      return [];
    }
    return [
      {
        time_start: candidate.time_start,
        time_end: candidate.time_end,
        text: candidate.text,
        checked: candidate.checked === true,
      },
    ];
  });
}

function parsePayload(value: unknown): LegacyTodayPayload {
  if (!isRecord(value) || !isRecord(value.now)) {
    throw new Error("Legacy Today response is not an object with a now block");
  }
  const iso = value.now.iso;
  const localHhmm = value.now.local_hhmm;
  const minutesIntoDay = value.now.minutes_into_day;
  if (
    typeof iso !== "string" ||
    !Number.isFinite(Date.parse(iso)) ||
    typeof localHhmm !== "string" ||
    !isFiniteNumber(minutesIntoDay) ||
    minutesIntoDay < 0 ||
    minutesIntoDay >= 24 * 60
  ) {
    throw new Error("Legacy Today response has invalid now metadata");
  }
  const workHours = value.work_hours;
  if (
    !Array.isArray(workHours) ||
    workHours.length !== 2 ||
    !isFiniteNumber(workHours[0]) ||
    !isFiniteNumber(workHours[1])
  ) {
    throw new Error("Legacy Today response has invalid work_hours metadata");
  }

  return {
    status: normalizeStatus(value.status),
    now: {
      iso,
      local_hhmm: localHhmm,
      minutes_into_day: minutesIntoDay,
    },
    work_hours: [workHours[0], workHours[1]],
    current_contexts: asStringArray(value.current_contexts),
    recommendations: asArray(value.recommendations),
    plan: parsePlan(value.plan),
    ...(value.plan_status === undefined ? {} : { plan_status: value.plan_status }),
    focused_count: isFiniteNumber(value.focused_count) ? value.focused_count : 0,
    calendar_event_count: isFiniteNumber(value.calendar_event_count)
      ? value.calendar_event_count
      : 0,
    active_contracts: asArray(value.active_contracts),
    contract_constraints: asArray(value.contract_constraints),
    engage_count: isFiniteNumber(value.engage_count) ? value.engage_count : 0,
    errors: asStringArray(value.errors),
  };
}

function localDateFor(iso: string, timezone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(iso));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((candidate) => candidate.type === type)?.value;
  const year = part("year");
  const month = part("month");
  const day = part("day");
  return year !== undefined && month !== undefined && day !== undefined
    ? `${year}-${month}-${day}`
    : iso.slice(0, 10);
}

function minutesFor(value: string): number | undefined {
  const match = /^(\d{1,2}):(\d{2})$/.exec(value);
  if (match === null) return undefined;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  return hours >= 0 && hours < 24 && minutes >= 0 && minutes < 60
    ? hours * 60 + minutes
    : undefined;
}

function instantAtLocalMinute(now: LegacyTodayNow, minute: number): string {
  const timestamp = Date.parse(now.iso) + (minute - now.minutes_into_day) * 60_000;
  return new Date(timestamp).toISOString();
}

function projectionHash(payload: LegacyTodayPayload): string {
  const text = JSON.stringify(payload);
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function itemId(item: LegacyTodayPlanItem, index: number): string {
  const text = `${index}|${item.time_start}|${item.time_end}|${item.text}`;
  let hash = 0;
  for (let cursor = 0; cursor < text.length; cursor += 1) {
    hash = Math.imul(31, hash) + text.charCodeAt(cursor);
  }
  return `legacy-plan:${index}:${(hash >>> 0).toString(16)}`;
}

function timelineItem(
  item: LegacyTodayPlanItem,
  index: number,
  now: LegacyTodayNow,
): DayTimelineItem | undefined {
  const startMinute = minutesFor(item.time_start);
  const rawEndMinute = minutesFor(item.time_end);
  if (startMinute === undefined || rawEndMinute === undefined) return undefined;
  const endMinute = rawEndMinute <= startMinute ? rawEndMinute + 24 * 60 : rawEndMinute;
  const startAt = instantAtLocalMinute(now, startMinute);
  const endAt = instantAtLocalMinute(now, endMinute);
  return {
    itemId: itemId(item, index),
    kind: "plan",
    title: item.text,
    detail: `${item.time_start}–${item.time_end} · legacy generated plan`,
    status: item.checked ? "completed" : "planned",
    mutability: Date.parse(endAt) <= Date.parse(now.iso) ? "past_protected" : "editable",
    precision: "derived",
    provenance: { source: "planner", label: "Legacy Today plan" },
    shape: "span",
    startAt,
    endAt,
  };
}

function dayWindow(payload: LegacyTodayPayload, timezone: string): TimelineDayWindow {
  const startMinute = payload.work_hours[0] * 60;
  const rawEndMinute = payload.work_hours[1] * 60;
  const endMinute = rawEndMinute <= startMinute ? rawEndMinute + 24 * 60 : rawEndMinute;
  const localDate = localDateFor(payload.now.iso, timezone);
  return {
    dayId: `legacy-today:${localDate}`,
    localDate,
    timezone,
    // Work hours are not silently relabeled as the missing native Journal boundary.
    dayBoundaryStart: "unknown",
    windowStart: instantAtLocalMinute(payload.now, startMinute),
    windowEnd: instantAtLocalMinute(payload.now, endMinute),
    now: payload.now.iso,
  };
}

function createModel(payload: LegacyTodayPayload, timezone: string): LegacyJournalViewModel {
  const revision = `legacy-today:${projectionHash(payload)}`;
  const day = dayWindow(payload, timezone);
  const timeline: DayTimelineInput = {
    instanceId: JOURNAL_INSTANCE_IDS.timeline,
    revision,
    day,
    access: { mode: "read_only", reason: READ_ONLY_REASON },
    renderMode: "timeline",
    density: "comfortable",
    items: payload.plan.flatMap((item, index) => {
      const mapped = timelineItem(item, index, payload.now);
      return mapped === undefined ? [] : [mapped];
    }),
  };
  const degradedIssues = payload.errors.map((message, index) => ({
    code: `legacy_today_error_${index + 1}`,
    message,
    affectedInstanceIds: [JOURNAL_WIDGET_INSTANCE_IDS.timeline],
  }));
  return {
    schemaVersion: 1,
    viewId: JOURNAL_VIEW_ID,
    revision,
    day,
    access: { mode: "read_only", reason: READ_ONLY_REASON },
    quality: {
      freshness: "current",
      observedAt: payload.now.iso,
      issues: [
        {
          code: "legacy_today_partial_projection",
          message:
            "Live legacy Today data is partial; native Journal behavior is unavailable.",
          affectedInstanceIds: [
            JOURNAL_WIDGET_INSTANCE_IDS.capture,
            JOURNAL_WIDGET_INSTANCE_IDS.timeline,
            JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
          ],
        },
        ...degradedIssues,
      ],
    },
    source: { kind: "live" },
    // Omitted inputs are important: the generic host renders those preserved slots as
    // unavailable instead of mounting a real renderer with fabricated/null data.
    widgetInputs: { [JOURNAL_INSTANCE_IDS.timeline]: timeline },
    legacy: {
      endpoint: LEGACY_TODAY_ENDPOINT,
      sourceStatus: payload.status,
      currentContexts: payload.current_contexts,
      recommendations: payload.recommendations,
      ...(payload.plan_status === undefined ? {} : { planStatus: payload.plan_status }),
      focusedCount: payload.focused_count,
      calendarEventCount: payload.calendar_event_count,
      activeContracts: payload.active_contracts,
      contractConstraints: payload.contract_constraints,
      engageCount: payload.engage_count,
      errors: payload.errors,
      timelineWindowSource: "legacy_work_hours",
      revisionSource: "adapter_projection_hash",
      limitations: LEGACY_JOURNAL_UNSUPPORTED,
    },
  };
}

function snapshotStatus(status: LegacyTodaySourceStatus): SnapshotStatus {
  return status === "error" ? "error" : "read-only";
}

function qualityMessage(status: LegacyTodaySourceStatus): string {
  return status === "degraded"
    ? "Partial/degraded legacy Today projection; native Journal behavior is unavailable."
    : status === "error"
      ? "Partial legacy Today projection reported an error."
      : "Partial read-only legacy Today projection; native Journal behavior is unavailable.";
}

function toSnapshot(model: LegacyJournalViewModel): LegacyJournalViewSnapshot {
  return {
    viewId: JOURNAL_VIEW_DEFINITION_ID,
    revision: model.revision,
    observedAt: model.quality.observedAt,
    status: snapshotStatus(model.legacy.sourceStatus),
    quality: { kind: "partial", message: qualityMessage(model.legacy.sourceStatus) },
    model,
    bindings: {
      [JOURNAL_BINDING_KEYS.day]: model.day,
      [JOURNAL_BINDING_KEYS.access]: model.access,
      [JOURNAL_BINDING_KEYS.quality]: model.quality,
      [JOURNAL_BINDING_KEYS.source]: model.source,
    },
    widgetInputs: model.widgetInputs,
  };
}

/**
 * Thin same-origin compatibility provider for the existing Flask Today read model.
 * It performs no direct App/System calls, opens no event stream, and maps no writes.
 */
export class LegacyFlaskViewAdapter implements ViewProvider {
  readonly appId = JOURNAL_APP_ID;

  readonly #fetch: typeof fetch;
  readonly #timezone: string;
  readonly #clock: () => string;
  #lastSnapshot: LegacyJournalViewSnapshot | undefined;

  constructor(options: LegacyFlaskViewAdapterOptions = {}) {
    this.#fetch = options.fetchImpl ?? fetch;
    this.#timezone =
      options.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? "UTC";
    this.#clock = options.clock ?? (() => new Date().toISOString());
  }

  async loadView(
    viewId: ViewId,
    _request: ViewLoadRequest,
  ): Promise<LegacyJournalViewSnapshot> {
    if (viewId !== JOURNAL_VIEW_DEFINITION_ID) {
      throw new Error(`LegacyFlaskViewAdapter cannot load view ${viewId}`);
    }
    const snapshot = await this.#readSnapshot();
    this.#lastSnapshot = snapshot;
    return snapshot;
  }

  async loadWidget(
    widgetTypeId: WidgetTypeId,
    request: WidgetLoadRequest,
  ): Promise<LegacyJournalWidgetSnapshot> {
    if (request.viewId !== JOURNAL_VIEW_DEFINITION_ID) {
      throw new Error(`LegacyFlaskViewAdapter cannot load widgets for ${request.viewId}`);
    }
    const expectedType = JOURNAL_WIDGET_TYPE_BY_INSTANCE.get(request.instanceId);
    const isTimeline =
      request.instanceId === JOURNAL_INSTANCE_IDS.timeline &&
      widgetTypeId === JOURNAL_WIDGET_TYPE_IDS.timeline &&
      expectedType === widgetTypeId;
    if (!isTimeline) {
      return {
        widgetTypeId,
        instanceId: request.instanceId,
        revision: this.#lastSnapshot?.revision,
        observedAt: this.#lastSnapshot?.observedAt ?? this.#clock(),
        status: "unavailable",
        quality: {
          kind: "partial",
          message: this.#widgetUnavailableReason(request.instanceId, widgetTypeId),
        },
        input: null,
      };
    }

    const snapshot = await this.loadView(JOURNAL_VIEW_DEFINITION_ID, {
      reason: "refresh",
      ...(request.knownRevision === undefined
        ? {}
        : { knownRevision: request.knownRevision }),
    });
    const input = snapshot.widgetInputs[JOURNAL_INSTANCE_IDS.timeline];
    return {
      widgetTypeId,
      instanceId: request.instanceId,
      revision: snapshot.revision,
      observedAt: snapshot.observedAt,
      status: input === undefined ? "unavailable" : snapshot.status,
      quality: snapshot.quality,
      input: input ?? null,
    };
  }

  async dispatch(intent: DashboardIntent): Promise<IntentResult> {
    return {
      intent_id: intent.intent_id,
      ...(intent.client_mutation_id === undefined
        ? {}
        : { client_mutation_id: intent.client_mutation_id }),
      status: "unavailable",
      revision: this.#lastSnapshot?.revision,
      message:
        intent.view_id === JOURNAL_VIEW_DEFINITION_ID
          ? READ_ONLY_REASON
          : "Intent targets a different view.",
    };
  }

  async reconcile(invalidation: AppInvalidation): Promise<ReconcileResult> {
    const matches =
      invalidation.appId === JOURNAL_APP_ID &&
      (invalidation.viewIds === undefined ||
        invalidation.viewIds.includes(JOURNAL_VIEW_DEFINITION_ID));
    if (!matches) {
      return { changed: false, revision: this.#lastSnapshot?.revision };
    }

    const previousRevision = this.#lastSnapshot?.revision;
    const snapshot = await this.#readSnapshot();
    this.#lastSnapshot = snapshot;
    const changed = previousRevision === undefined || snapshot.revision !== previousRevision;
    return changed
      ? { changed: true, revision: snapshot.revision, snapshot }
      : { changed: false, revision: snapshot.revision };
  }

  async #readSnapshot(): Promise<LegacyJournalViewSnapshot> {
    try {
      const response = await this.#fetch(LEGACY_TODAY_ENDPOINT, {
        method: "GET",
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error(`Legacy Today endpoint returned HTTP ${response.status}`);
      }
      const model = createModel(parsePayload(await response.json()), this.#timezone);
      return toSnapshot(model);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return {
        viewId: JOURNAL_VIEW_DEFINITION_ID,
        observedAt: this.#clock(),
        status: "unavailable",
        quality: {
          kind: "partial",
          message: `Legacy Today endpoint unavailable: ${message}`,
        },
        model: null,
        bindings: {},
        widgetInputs: {},
      };
    }
  }

  #widgetUnavailableReason(instanceId: string, widgetTypeId: WidgetTypeId): string {
    if (instanceId === JOURNAL_INSTANCE_IDS.capture) {
      return LEGACY_JOURNAL_UNSUPPORTED.find(
        (item) => item.capability === "capture_persistence",
      )?.reason ?? READ_ONLY_REASON;
    }
    if (instanceId === JOURNAL_INSTANCE_IDS.runningNotes) {
      return LEGACY_JOURNAL_UNSUPPORTED.find(
        (item) => item.capability === "running_notes_records",
      )?.reason ?? READ_ONLY_REASON;
    }
    return `Widget ${widgetTypeId} is not bound to Journal instance ${instanceId}.`;
  }
}
