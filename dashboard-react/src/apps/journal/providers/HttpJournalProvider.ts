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
import {
  initializeLocalIdentity,
  issueHumanGesture,
  localIdentityHeaders,
  sha256Hex,
} from "../../../security/localIdentity";
import {
  JOURNAL_APP_ID,
  JOURNAL_BINDING_KEYS,
  JOURNAL_INSTANCE_IDS,
  JOURNAL_VIEW_DEFINITION_ID,
  JOURNAL_WIDGET_TYPE_BY_INSTANCE,
  type JournalWidgetInput,
} from "../bindings";
import {
  JOURNAL_VIEW_ID,
  JOURNAL_WIDGET_INSTANCE_IDS,
  type CaptureAnnotation,
  type JournalCaptureMode,
  type CapturePlacementStatus,
  type CaptureProcessingStatus,
  type JournalCaptureInput,
  type JournalCaptureSubmission,
  type JournalCaptureTarget,
  type JournalDataIssue,
  type JournalDayBinding,
  type JournalRunningNoteItem,
  type JournalRunningNotesInput,
  type JournalTimelineInput,
  type JournalViewModel,
} from "../contracts";
import {
  LegacyFlaskViewAdapter,
  type LegacyJournalViewSnapshot,
} from "./LegacyFlaskViewAdapter";

export const JOURNAL_VIEW_ENDPOINT = "/api/journal/view" as const;
export const JOURNAL_CAPTURE_ENDPOINT = "/api/journal/captures" as const;
export const JOURNAL_RUNNING_NOTE_COWORK_ENDPOINT =
  "/api/journal/running-notes" as const;

type HttpJournalViewSnapshot = ViewSnapshot<
  JournalViewModel | null,
  JournalDayBinding | JournalViewModel["access"] | JournalViewModel["quality"] | JournalViewModel["source"],
  JournalWidgetInput
>;

type HttpJournalWidgetSnapshot = WidgetSnapshot<JournalWidgetInput | null>;

export type HttpJournalProviderOptions = {
  readonly fetchImpl?: typeof fetch;
  readonly legacyProvider?: LegacyFlaskViewAdapter;
  readonly clock?: () => string;
  readonly navigate?: (href: string) => void;
};

type NativeJournalPayload = {
  readonly schemaVersion: 1;
  readonly revision: string;
  readonly observedAt: string;
  readonly day: JournalDayBinding;
  readonly access: JournalViewModel["access"];
  readonly quality: JournalViewModel["quality"];
  readonly source: { readonly kind: "live" };
  readonly capture: JournalCaptureInput;
  readonly runningNotes: JournalRunningNotesInput;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function string(value: unknown, label: string): string {
  if (typeof value !== "string") throw new Error(`Journal response has invalid ${label}`);
  return value;
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function sha256(value: unknown, label: string): string {
  const candidate = string(value, label);
  if (!/^[0-9a-f]{64}$/.test(candidate)) {
    throw new Error(`Journal response has invalid ${label}`);
  }
  return candidate;
}

function strings(value: unknown): readonly string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new Error("Journal response has an invalid string collection");
  }
  return value;
}

function access(value: unknown): JournalViewModel["access"] {
  if (!isRecord(value) || (value.mode !== "read_write" && value.mode !== "read_only")) {
    throw new Error("Journal response has invalid access");
  }
  if (value.mode === "read_only") {
    return { mode: "read_only", reason: string(value.reason, "read-only reason") };
  }
  return { mode: "read_write" };
}

function day(value: unknown): JournalDayBinding {
  if (!isRecord(value)) throw new Error("Journal response has invalid day");
  return {
    dayId: string(value.dayId, "day ID"),
    localDate: string(value.localDate, "local date"),
    timezone: string(value.timezone, "timezone"),
    dayBoundaryStart: string(value.dayBoundaryStart, "day boundary"),
    windowStart: string(value.windowStart, "window start"),
    windowEnd: string(value.windowEnd, "window end"),
    now: string(value.now, "current time"),
  };
}

function annotation(value: unknown): CaptureAnnotation | undefined {
  if (!isRecord(value)) return undefined;
  const effects = strings(value.effects);
  return { summary: string(value.summary, "annotation summary"), effects };
}

function captureTarget(value: unknown): JournalCaptureTarget {
  if (!isRecord(value) || !Array.isArray(value.supportedModes)) {
    throw new Error("Journal response has invalid capture target");
  }
  const targetId = value.targetId;
  if (targetId !== "auto" && targetId !== "log" && targetId !== "running_notes") {
    throw new Error("Journal response has invalid capture destination");
  }
  const modes = strings(value.supportedModes);
  if (modes.some((mode) => mode !== "dumb" && mode !== "smart")) {
    throw new Error("Journal response has invalid capture mode");
  }
  const defaultMode = value.defaultMode;
  if (defaultMode !== "dumb" && defaultMode !== "smart") {
    throw new Error("Journal response has invalid default mode");
  }
  return {
    targetId,
    label: string(value.label, "capture label"),
    description: string(value.description, "capture description"),
    supportedModes: modes as readonly JournalCaptureMode[],
    defaultMode,
    enabled: value.enabled === true,
    ...(optionalString(value.unavailableReason) === undefined
      ? {}
      : { unavailableReason: optionalString(value.unavailableReason) }),
  };
}

function captureSubmission(value: unknown): JournalCaptureSubmission {
  if (!isRecord(value)) throw new Error("Journal response has invalid capture");
  const targetId = value.targetId;
  const mode = value.mode;
  const persistence = value.persistenceStatus;
  const placement = value.placementStatus;
  const processing = value.processingStatus;
  if (targetId !== "auto" && targetId !== "log" && targetId !== "running_notes") {
    throw new Error("Journal response has invalid capture destination");
  }
  if (mode !== "dumb" && mode !== "smart") throw new Error("Journal response has invalid capture mode");
  if (persistence !== "persisted" && persistence !== "failed") {
    throw new Error("Journal response has invalid persistence status");
  }
  if (placement !== "pending" && placement !== "placed" && placement !== "failed") {
    throw new Error("Journal response has invalid placement status");
  }
  if (
    processing !== "not_requested" &&
    processing !== "pending" &&
    processing !== "running" &&
    processing !== "succeeded" &&
    processing !== "failed"
  ) {
    throw new Error("Journal response has invalid processing status");
  }
  return {
    captureId: optionalString(value.captureId),
    clientMutationId: string(value.clientMutationId, "mutation ID"),
    targetId,
    mode,
    ...(typeof value.exactText === "string" ? { exactText: value.exactText } : {}),
    submittedAt: string(value.submittedAt, "submission time"),
    persistenceStatus: persistence,
    placementStatus: placement as CapturePlacementStatus,
    processingStatus: processing as CaptureProcessingStatus,
    ...(annotation(value.annotation) === undefined
      ? {}
      : { annotation: annotation(value.annotation) }),
    ...(optionalString(value.errorMessage) === undefined
      ? {}
      : { errorMessage: optionalString(value.errorMessage) }),
    ...(optionalString(value.sourceRef) === undefined
      ? {}
      : { sourceRef: optionalString(value.sourceRef) }),
  };
}

function captureInput(value: unknown): JournalCaptureInput {
  if (!isRecord(value) || !Array.isArray(value.targets) || !Array.isArray(value.recentSubmissions)) {
    throw new Error("Journal response has invalid capture input");
  }
  const smartHelp = value.smartHelp;
  if (
    smartHelp !== null &&
    smartHelp !== undefined &&
    (!isRecord(smartHelp) ||
      typeof smartHelp.summary !== "string" ||
      typeof smartHelp.details !== "string")
  ) {
    throw new Error("Journal response has invalid Smart disclosure");
  }
  return {
    instanceId: JOURNAL_WIDGET_INSTANCE_IDS.capture,
    revision: string(value.revision, "capture revision"),
    dayId: string(value.dayId, "capture day"),
    access: access(value.access),
    ...(isRecord(smartHelp)
      ? {
          smartHelp: {
            summary: string(smartHelp.summary, "Smart help summary"),
            details: string(smartHelp.details, "Smart help details"),
          },
        }
      : {}),
    targets: value.targets.map(captureTarget),
    capturesToday: typeof value.capturesToday === "number" ? value.capturesToday : 0,
    recentSubmissions: value.recentSubmissions.map(captureSubmission),
  };
}

function runningNote(value: unknown): JournalRunningNoteItem {
  if (!isRecord(value) || !isRecord(value.processing) || !isRecord(value.provenance)) {
    throw new Error("Journal response has invalid Running Note");
  }
  const captureMode = value.captureMode;
  const processing = value.processing.state;
  const resolution = value.resolutionState;
  const provenanceSource = value.provenance.source;
  if (captureMode !== "dumb" && captureMode !== "smart") {
    throw new Error("Journal response has invalid note capture mode");
  }
  if (
    processing !== "not_requested" &&
    processing !== "pending" &&
    processing !== "running" &&
    processing !== "succeeded" &&
    processing !== "failed"
  ) {
    throw new Error("Journal response has invalid note processing state");
  }
  if (
    resolution !== "open" &&
    resolution !== "routed_to_task" &&
    resolution !== "routed_to_consideration" &&
    resolution !== "appended" &&
    resolution !== "dismissed"
  ) {
    throw new Error("Journal response has invalid note resolution state");
  }
  if (provenanceSource !== "local_submission") {
    throw new Error("Journal response has invalid note provenance");
  }
  let document: JournalRunningNoteItem["document"];
  if (value.document !== undefined) {
    if (!isRecord(value.document)) {
      throw new Error("Journal response has invalid note document state");
    }
    const gestureContextSha256 = sha256(
      value.document.gestureContextSha256,
      "note document gesture context",
    );
    if (value.document.state === "available") {
      document = { state: "available", gestureContextSha256 };
    } else if (
      value.document.state === "current" ||
      value.document.state === "paused_diverged"
    ) {
      const epoch = value.document.contentAuthorityEpoch;
      if (typeof epoch !== "number" || !Number.isInteger(epoch) || epoch < 1) {
        throw new Error("Journal response has invalid content authority epoch");
      }
      document = {
        state: value.document.state,
        gestureContextSha256,
        href: string(value.document.href, "Co-work link"),
        storeId: string(value.document.storeId, "Co-work store ID"),
        documentId: string(value.document.documentId, "Co-work document ID"),
        changeId: string(value.document.changeId, "Co-work change ID"),
        contentAuthorityEpoch: epoch,
      };
    } else {
      throw new Error("Journal response has invalid note document state");
    }
  }
  return {
    itemId: string(value.itemId, "note ID"),
    markdown: string(value.markdown, "note Markdown"),
    createdAt: string(value.createdAt, "note created time"),
    updatedAt: string(value.updatedAt, "note updated time"),
    provenance: {
      source: provenanceSource,
      label: string(value.provenance.label, "note provenance"),
      ...(optionalString(value.provenance.actor) ? { actor: optionalString(value.provenance.actor) } : {}),
    },
    captureMode,
    processing: {
      state: processing as JournalRunningNoteItem["processing"]["state"],
      ...(annotation(value.processing.annotation) === undefined
        ? {}
        : { annotation: annotation(value.processing.annotation) }),
      ...(optionalString(value.processing.errorMessage) === undefined
        ? {}
        : { errorMessage: optionalString(value.processing.errorMessage) }),
    },
    resolutionState: resolution,
    version: typeof value.version === "number" ? value.version : 1,
    ...(document === undefined ? {} : { document }),
  };
}

function runningNotesInput(value: unknown): JournalRunningNotesInput {
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw new Error("Journal response has invalid Running Notes input");
  }
  return {
    instanceId: JOURNAL_WIDGET_INSTANCE_IDS.runningNotes,
    revision: string(value.revision, "notes revision"),
    dayId: string(value.dayId, "notes day"),
    access: access(value.access),
    displayMode: value.displayMode === "grouped" ? "grouped" : "chronological",
    items: value.items.map(runningNote),
  };
}

function nativePayload(value: unknown): NativeJournalPayload {
  if (!isRecord(value) || value.ok !== true || !isRecord(value.view)) {
    throw new Error("Journal endpoint returned an invalid response");
  }
  const view = value.view;
  if (!isRecord(view.quality) || !Array.isArray(view.quality.issues)) {
    throw new Error("Journal endpoint returned invalid quality metadata");
  }
  const issues = view.quality.issues.filter(isRecord).map((issue): JournalDataIssue => ({
    code: string(issue.code, "issue code"),
    message: string(issue.message, "issue message"),
    affectedInstanceIds: Array.isArray(issue.affectedInstanceIds)
      ? (strings(issue.affectedInstanceIds) as JournalDataIssue["affectedInstanceIds"])
      : [],
  }));
  return {
    schemaVersion: 1,
    revision: string(view.revision, "revision"),
    observedAt: string(view.observedAt, "observed time"),
    day: day(view.day),
    access: access(view.access),
    quality: {
      freshness:
        view.quality.freshness === "stale" || view.quality.freshness === "offline"
          ? view.quality.freshness
          : "current",
      observedAt: string(view.quality.observedAt, "quality observed time"),
      issues,
    },
    source: { kind: "live" },
    capture: captureInput(view.capture),
    runningNotes: runningNotesInput(view.runningNotes),
  };
}

function emptyTimeline(native: NativeJournalPayload): JournalTimelineInput {
  return {
    instanceId: JOURNAL_WIDGET_INSTANCE_IDS.timeline,
    revision: native.revision,
    day: native.day,
    access: { mode: "read_only", reason: "Live Today timeline data is unavailable." },
    renderMode: "timeline",
    density: "comfortable",
    items: [],
  };
}

function timelineFromLegacy(
  legacy: LegacyJournalViewSnapshot,
  native: NativeJournalPayload,
): JournalTimelineInput {
  const candidate = legacy.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline];
  if (candidate === undefined || candidate.day.dayId !== native.day.dayId) {
    return emptyTimeline(native);
  }
  return {
    ...candidate,
    revision: native.revision,
    day: native.day,
    access: candidate.access ?? { mode: "read_only", reason: "Legacy Today timeline." },
  } as JournalTimelineInput;
}

function bindings(model: JournalViewModel) {
  return {
    [JOURNAL_BINDING_KEYS.day]: model.day,
    [JOURNAL_BINDING_KEYS.access]: model.access,
    [JOURNAL_BINDING_KEYS.quality]: model.quality,
    [JOURNAL_BINDING_KEYS.source]: model.source,
  };
}

function snapshotStatus(model: JournalViewModel): SnapshotStatus {
  if (model.access.mode === "read_only") return "read-only";
  if (model.quality.freshness === "offline") return "offline";
  if (model.quality.freshness === "stale") return "stale";
  return "ready";
}

export async function journalCaptureGestureContext(payload: {
  readonly client_mutation_id: string;
  readonly day_id: string;
  readonly target_id: string;
  readonly mode: string;
  readonly exact_text: string;
  readonly input_mode: string;
  readonly stated_at?: string;
}): Promise<string> {
  const exactTextSha256 = await sha256Hex(payload.exact_text);
  const canonical = JSON.stringify({
    client_mutation_id: payload.client_mutation_id,
    day_id: payload.day_id,
    exact_text_sha256: exactTextSha256,
    input_mode: payload.input_mode,
    mode: payload.mode,
    schema: "wb.journal-capture-gesture/v1",
    stated_at: payload.stated_at ?? null,
    target_id: payload.target_id,
  });
  return sha256Hex(canonical);
}

export class HttpJournalProvider implements ViewProvider {
  readonly appId = JOURNAL_APP_ID;
  readonly #fetch: typeof fetch;
  readonly #legacy: LegacyFlaskViewAdapter;
  readonly #clock: () => string;
  readonly #navigate: (href: string) => void;
  #last: HttpJournalViewSnapshot | undefined;

  constructor(options: HttpJournalProviderOptions = {}) {
    this.#fetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.#legacy = options.legacyProvider ?? new LegacyFlaskViewAdapter({ fetchImpl: this.#fetch });
    this.#clock = options.clock ?? (() => new Date().toISOString());
    this.#navigate = options.navigate ?? ((href) => window.location.assign(href));
  }

  async loadView(viewId: ViewId, _request: ViewLoadRequest): Promise<HttpJournalViewSnapshot> {
    if (viewId !== JOURNAL_VIEW_DEFINITION_ID) {
      throw new Error(`HttpJournalProvider cannot load view ${viewId}`);
    }
    const identity = await initializeLocalIdentity({ fetchImpl: this.#fetch });
    const [legacy, native] = await Promise.all([
      this.#legacy.loadView(viewId, { reason: "refresh" }),
      this.#readNative(),
    ]);
    const canWrite = identity.authenticated && native.access.mode === "read_write";
    const identityReason = identity.authenticated ? undefined : identity.reason;
    const writeAccess = canWrite
      ? ({ mode: "read_write" } as const)
      : ({
          mode: "read_only",
          reason:
            native.access.mode === "read_only"
              ? native.access.reason
              : identityReason ?? "Open Journal through the local Work Buddy launcher to capture.",
        } as const);
    const issues = [...native.quality.issues];
    if (legacy.model === null) {
      issues.push({
        code: "legacy_today_unavailable",
        message: legacy.quality.message ?? "Live Today timeline data is unavailable.",
        affectedInstanceIds: [JOURNAL_WIDGET_INSTANCE_IDS.timeline],
      });
    }
    const model: JournalViewModel = {
      schemaVersion: 1,
      viewId: JOURNAL_VIEW_ID,
      revision: `${native.revision}:${legacy.revision ?? "timeline-unavailable"}`,
      day: native.day,
      access: writeAccess,
      quality: { ...native.quality, issues },
      source: { kind: "live" },
      widgetInputs: {
        [JOURNAL_WIDGET_INSTANCE_IDS.capture]: {
          ...native.capture,
          revision: native.revision,
          access: writeAccess,
        },
        [JOURNAL_WIDGET_INSTANCE_IDS.timeline]: timelineFromLegacy(legacy, native),
        [JOURNAL_WIDGET_INSTANCE_IDS.runningNotes]: native.runningNotes,
      },
    };
    const widgetInputs: Readonly<Record<string, JournalWidgetInput>> = {
      ...model.widgetInputs,
    };
    const snapshot: HttpJournalViewSnapshot = {
      viewId: JOURNAL_VIEW_DEFINITION_ID,
      revision: model.revision,
      observedAt: native.observedAt,
      status: snapshotStatus(model),
      quality: {
        kind: issues.length === 0 ? "complete" : "partial",
        ...(issues.length === 0 ? {} : { message: issues.map((issue) => issue.message).join(" ") }),
      },
      model,
      bindings: bindings(model),
      widgetInputs,
    };
    this.#last = snapshot;
    return snapshot;
  }

  async loadWidget(
    widgetTypeId: WidgetTypeId,
    request: WidgetLoadRequest,
  ): Promise<HttpJournalWidgetSnapshot> {
    if (request.viewId !== JOURNAL_VIEW_DEFINITION_ID) {
      throw new Error(`HttpJournalProvider cannot load widgets for ${request.viewId}`);
    }
    const expected = JOURNAL_WIDGET_TYPE_BY_INSTANCE.get(request.instanceId);
    if (expected === undefined || expected !== widgetTypeId) {
      return {
        widgetTypeId,
        instanceId: request.instanceId,
        status: "unavailable",
        observedAt: this.#clock(),
        quality: { kind: "partial", message: "Widget is not bound to this Journal slot." },
        input: null,
      };
    }
    const snapshot =
      this.#last ??
      (await this.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" }));
    const input = snapshot.widgetInputs[request.instanceId];
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
    if (intent.view_id !== JOURNAL_VIEW_DEFINITION_ID) {
      return this.#result(intent, "rejected", "Intent targets a different view.");
    }
    if (
      intent.intent_type === "wb.notes.open-document-requested" &&
      intent.instance_id === JOURNAL_INSTANCE_IDS.runningNotes &&
      isRecord(intent.payload)
    ) {
      return this.#openRunningNoteDocument(intent);
    }
    if (
      intent.intent_type !== "wb.capture.submit" ||
      intent.instance_id !== JOURNAL_INSTANCE_IDS.capture ||
      intent.client_mutation_id === undefined ||
      !isRecord(intent.payload)
    ) {
      return this.#result(
        intent,
        "unavailable",
        intent.intent_type.startsWith("wb.notes.")
          ? "Captured notes remain editable in the authoritative daily note during migration."
          : "That live Journal action is not available yet.",
      );
    }
    const exactText = intent.payload.exact_text;
    if (typeof exactText !== "string") {
      return this.#result(intent, "rejected", "Capture text is required.");
    }
    const body = {
      client_mutation_id: intent.client_mutation_id,
      day_id: string(intent.payload.day_id, "capture day"),
      target_id: string(intent.payload.target_id, "capture destination"),
      mode: string(intent.payload.mode, "capture mode"),
      exact_text: exactText,
      input_mode: "unknown",
      ...(typeof intent.payload.stated_at === "string"
        ? { stated_at: intent.payload.stated_at }
        : {}),
    };
    try {
      const identity = await initializeLocalIdentity({ fetchImpl: this.#fetch });
      if (!identity.authenticated) {
        return this.#result(
          intent,
          "unavailable",
          identity.reason ?? "An authenticated local Journal session is required.",
        );
      }
      const contextSha256 = await journalCaptureGestureContext(body);
      const gesture = await issueHumanGesture(
        {
          action: "journal.capture.submit",
          subject: `journal-capture:${intent.client_mutation_id}`,
          contextSha256,
        },
        this.#fetch,
      );
      const response = await this.#fetch(JOURNAL_CAPTURE_ENDPOINT, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          ...localIdentityHeaders(gesture.token),
        },
        body: JSON.stringify(body),
      });
      const payload = (await response.json()) as unknown;
      if (!response.ok || !isRecord(payload) || payload.ok !== true || !isRecord(payload.capture)) {
        const message =
          isRecord(payload) && isRecord(payload.error) && typeof payload.error.message === "string"
            ? payload.error.message
            : "Journal could not save that capture.";
        return this.#result(
          intent,
          response.status === 409 ? "conflict" : "rejected",
          message,
        );
      }
      const accepted = captureSubmission(payload.capture);
      return this.#result(intent, "accepted", "Exact text persisted.", accepted.captureId);
    } catch (error) {
      return this.#result(
        intent,
        "unavailable",
        error instanceof Error ? error.message : "Journal capture is unavailable.",
      );
    }
  }

  async #openRunningNoteDocument(intent: DashboardIntent): Promise<IntentResult> {
    if (!isRecord(intent.payload)) {
      return this.#result(intent, "rejected", "The Running Note action is invalid.");
    }
    const itemId = intent.payload.item_id;
    const expectedVersion = intent.payload.expected_version;
    const contextSha256 = intent.payload.gesture_context_sha256;
    if (
      typeof itemId !== "string" ||
      !itemId ||
      typeof expectedVersion !== "number" ||
      !Number.isInteger(expectedVersion) ||
      expectedVersion < 1 ||
      typeof contextSha256 !== "string" ||
      !/^[0-9a-f]{64}$/.test(contextSha256)
    ) {
      return this.#result(intent, "rejected", "The Running Note action is invalid.");
    }
    try {
      const identity = await initializeLocalIdentity({ fetchImpl: this.#fetch });
      if (!identity.authenticated) {
        return this.#result(
          intent,
          "unavailable",
          identity.reason ?? "An authenticated local Journal session is required.",
        );
      }
      const gesture = await issueHumanGesture(
        {
          action: "journal.running_note.open_in_cowork",
          subject: `journal-running-note:${itemId}`,
          contextSha256,
        },
        this.#fetch,
      );
      const response = await this.#fetch(
        `${JOURNAL_RUNNING_NOTE_COWORK_ENDPOINT}/${encodeURIComponent(itemId)}/open-in-cowork`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            ...localIdentityHeaders(gesture.token),
          },
          body: JSON.stringify({ expected_version: expectedVersion }),
        },
      );
      const payload = (await response.json()) as unknown;
      if (
        !response.ok ||
        !isRecord(payload) ||
        payload.ok !== true ||
        typeof payload.coworkHref !== "string" ||
        !payload.coworkHref.startsWith("/app/cowork?")
      ) {
        const message =
          isRecord(payload) && isRecord(payload.error) && typeof payload.error.message === "string"
            ? payload.error.message
            : "Co-work could not open that Running Note.";
        return this.#result(
          intent,
          response.status === 409 ? "conflict" : "rejected",
          message,
        );
      }
      this.#last = undefined;
      this.#navigate(payload.coworkHref);
      return this.#result(intent, "accepted", "Opening the Co-work document.");
    } catch (error) {
      return this.#result(
        intent,
        "unavailable",
        error instanceof Error ? error.message : "Co-work is unavailable.",
      );
    }
  }

  async reconcile(invalidation: AppInvalidation): Promise<ReconcileResult> {
    if (
      invalidation.appId !== JOURNAL_APP_ID ||
      (invalidation.viewIds !== undefined && !invalidation.viewIds.includes(JOURNAL_VIEW_DEFINITION_ID))
    ) {
      return { changed: false, revision: this.#last?.revision };
    }
    const prior = this.#last?.revision;
    const snapshot = await this.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "reconcile" });
    return prior === snapshot.revision
      ? { changed: false, revision: snapshot.revision }
      : { changed: true, revision: snapshot.revision, snapshot };
  }

  async #readNative(): Promise<NativeJournalPayload> {
    const response = await this.#fetch(JOURNAL_VIEW_ENDPOINT, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`Journal endpoint returned HTTP ${response.status}`);
    return nativePayload(await response.json());
  }

  #result(
    intent: DashboardIntent,
    status: IntentResult["status"],
    message: string,
    revision?: string,
  ): IntentResult {
    return {
      intent_id: intent.intent_id,
      ...(intent.client_mutation_id === undefined
        ? {}
        : { client_mutation_id: intent.client_mutation_id }),
      status,
      ...(revision === undefined ? {} : { revision }),
      message,
    };
  }
}
