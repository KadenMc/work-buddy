import type {
  AppInvalidation,
  DefaultWidgetSlot,
  DashboardIntent,
  EffectiveViewComposition,
  IntentResult,
  ReconcileResult,
  SnapshotStatus,
  ViewId,
  ViewLoadRequest,
  ViewSnapshot,
  WidgetLoadRequest,
  WidgetSnapshot,
  WidgetTypeId,
  JsonValue,
} from "../../../dashboard/contributions/contracts";
import {
  asWidgetInstanceId,
  asWidgetSlotId,
} from "../../../dashboard/contributions/contracts";
import type { ViewProvider } from "../../../dashboard/providers/ViewProvider";
import type { CaptureFollowUp, CaptureSmartAvailability } from "../../../widget-library/capture/contracts";
import { safeCaptureAppHref } from "../../../widget-library/capture/FollowUpLinks";
import {
  initializeLocalIdentity,
  issueHumanGesture,
  localIdentityHeaders,
  sha256Hex,
} from "../../../security/localIdentity";
import { exactHumanAuthorityHeaders } from "../../../security/humanAuthority";
import {
  JOURNAL_APP_ID,
  JOURNAL_BINDING_KEYS,
  JOURNAL_GENERIC_ROLE_ID,
  JOURNAL_GENERIC_WIDGET_TYPE_ID,
  JOURNAL_INSTANCE_IDS,
  JOURNAL_ROLE_IDS,
  JOURNAL_VIEW_DEFINITION_ID,
  JOURNAL_WIDGET_TYPE_BY_INSTANCE,
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
  type JournalEffectiveComposition,
  type JournalEffectiveFieldInput,
  type JournalEffectiveModule,
  type JournalDocumentModuleState,
  type JournalFieldValue,
  type JournalFieldReference,
  type JournalFieldValueKind,
  type JournalGenericModuleInput,
  type JournalNativeItemInput,
  type JournalItemAction,
  type JournalItemRelation,
  type JournalPromptGenerationRequest,
  type JournalPromptInteraction,
  type JournalPromptResultVariant,
  type JournalRunningNoteItem,
  type JournalRunningNotesInput,
  type JournalTimelineInput,
  type JournalViewModel,
  type JournalWidgetInput,
} from "../contracts";
import {
  LegacyFlaskViewAdapter,
  type LegacyJournalViewSnapshot,
} from "./LegacyFlaskViewAdapter";

export const JOURNAL_VIEW_ENDPOINT = "/api/journal/view" as const;
export const JOURNAL_CAPTURE_ENDPOINT = "/api/journal/captures" as const;
export const JOURNAL_FIELD_VALUES_ENDPOINT = "/api/journal/field-values" as const;
export const JOURNAL_ITEMS_ENDPOINT = "/api/journal/items" as const;
export const JOURNAL_PROMPT_INTERACTIONS_ENDPOINT =
  "/api/journal/prompt-interactions" as const;
export const JOURNAL_RUNNING_NOTE_COWORK_ENDPOINT =
  "/api/journal/running-notes" as const;
export const JOURNAL_DOCUMENT_MODULE_ENDPOINT =
  "/api/journal/document-modules" as const;

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
  readonly effectiveComposition?: JournalEffectiveComposition;
  readonly logEntries: readonly NativeJournalLogEntry[];
  readonly fieldValues: readonly NativeJournalFieldValue[];
  readonly nativeItems: readonly NativeJournalModuleItem[];
  readonly promptInteractions: readonly JournalPromptInteraction[];
};

type NativeJournalLogEntry = {
  readonly itemId: string;
  readonly text: string;
  readonly markdown: string;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly revision: number;
  readonly lifecycle: string;
  readonly authorityKind: string;
  readonly sourceRef?: string;
  readonly moduleInstanceId?: string;
  readonly moduleInstanceVersion?: number;
};

type NativeJournalFieldValue = {
  readonly valueId: string;
  readonly moduleInstanceId: string;
  readonly moduleInstanceVersion: number;
  readonly fieldId: string;
  readonly fieldDefinitionVersion: number;
  readonly valueKind: JournalFieldValueKind;
  readonly disposition?: "missing" | "skipped" | "declined";
  readonly value: JournalFieldValue;
  readonly currentRevision: number;
  readonly authorship: string;
  readonly reviewState: string;
  readonly sourceRef?: string;
  readonly lifecycle: string;
};

type NativeJournalModuleItem = JournalNativeItemInput & {
  readonly moduleInstanceId?: string;
  readonly moduleInstanceVersion?: number;
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
    ...(optionalString(value.openedAt) ? { openedAt: optionalString(value.openedAt) } : {}),
    ...(optionalString(value.closedAt) ? { closedAt: optionalString(value.closedAt) } : {}),
    now: string(value.now, "current time"),
  };
}

function annotation(value: unknown): CaptureAnnotation | undefined {
  if (!isRecord(value)) return undefined;
  const effects = strings(value.effects);
  return { summary: string(value.summary, "annotation summary"), effects };
}

function followUps(value: unknown): readonly CaptureFollowUp[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.length > 1) throw new Error("Journal response has invalid follow-ups");
  return value.map((item): CaptureFollowUp => {
    if (!isRecord(item)) throw new Error("Journal response has invalid follow-up");
    if (item.kind === "status" && (item.status === "pending" || item.status === "failed")) {
      return { kind: "status", status: item.status, label: string(item.label, "follow-up label") };
    }
    const href = string(item.href, "follow-up link");
    const referenceId = string(item.referenceId, "follow-up reference");
    if (item.kind !== "app_link" || !/^th-[0-9a-f]{8}$/.test(referenceId)
        || !safeCaptureAppHref(href)) throw new Error("Journal response has unsafe follow-up link");
    const parsed = new URL(href, "https://work-buddy.invalid");
    const pairs = [...parsed.searchParams.entries()];
    if (parsed.pathname !== "/app/tasks" || pairs.length !== 1
        || !((pairs[0][0] === "proposal" && pairs[0][1] === referenceId)
          || (pairs[0][0] === "task" && /^t-[0-9a-f]{8}$/.test(pairs[0][1])))) {
      throw new Error("Journal response has unsafe follow-up link");
    }
    return { kind: "app_link", referenceId, href, label: string(item.label, "follow-up label"),
      ...(typeof item.description === "string" ? { description: item.description } : {}) };
  });
}

function smartAvailability(value: unknown): CaptureSmartAvailability | undefined {
  if (value === undefined || value === null) return undefined;
  if (!isRecord(value) || !isRecord(value.disclosure)
      || !["disabled_by_policy", "provider_unavailable", "ready"].includes(String(value.state))
      || value.disclosure.tools !== false || value.disclosure.web !== false
      || value.disclosure.maxInputBytes !== 32768) throw new Error("Journal response has invalid Smart availability");
  const provider = optionalString(value.disclosure.provider) ?? null;
  const model = optionalString(value.disclosure.model) ?? null;
  if (value.state === "ready" && (!provider || !model)) throw new Error("Smart is missing provider/model disclosure");
  let action: CaptureSmartAvailability["action"];
  if (isRecord(value.action)) {
    if (value.action.kind === "retry") action = { kind: "retry", label: string(value.action.label, "Smart action") };
    else if (value.action.kind === "app_link"
        && value.action.href === "/app/settings/apps/journal?setting=wb.journal.smart-processing") {
      action = { kind: "app_link", label: string(value.action.label, "Smart action"), href: value.action.href };
    } else throw new Error("Journal response has invalid Smart action");
  }
  return { state: value.state as CaptureSmartAvailability["state"], code: string(value.code, "Smart availability code"),
    reason: string(value.reason, "Smart availability reason"),
    disclosure: { provider, model, maxInputBytes: 32768, tools: false, web: false },
    ...(action ? { action } : {}) };
}

function captureTarget(value: unknown): JournalCaptureTarget {
  if (!isRecord(value) || !Array.isArray(value.supportedModes)) {
    throw new Error("Journal response has invalid capture target");
  }
  const targetId = value.targetId;
  if (typeof targetId !== "string" || targetId.trim().length === 0) {
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
  if (typeof targetId !== "string" || targetId.trim().length === 0) {
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
    followUps: followUps(value.followUps),
    retryable: value.retryable === true,
    ...(typeof value.revision === "number" && Number.isInteger(value.revision) && value.revision >= 1
      ? { revision: value.revision } : {}),
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
  const availability = smartAvailability(value.smartAvailability);
  const secondaryActions = value.secondaryActions;
  if (secondaryActions !== undefined && (!Array.isArray(secondaryActions)
      || secondaryActions.length > 1 || secondaryActions.some((item) => !isRecord(item)
        || item.actionId !== "task_proposal" || item.targetId !== "running_notes" || item.mode !== "dumb"
        || typeof item.label !== "string" || typeof item.description !== "string"))) {
    throw new Error("Journal response has invalid secondary capture action");
  }
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
    ...(availability ? { smartAvailability: availability } : {}),
    ...(Array.isArray(secondaryActions) ? { secondaryActions: secondaryActions.map((item) => ({
      actionId: "task_proposal", targetId: "running_notes", mode: "dumb" as const,
      label: String(item.label), description: String(item.description),
    })) } : {}),
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
    // The shared composer consumes chronological records (newest at the end),
    // while the native API returns its bounded newest-first database window.
    recentSubmissions: value.recentSubmissions.map(captureSubmission)
      .sort((left, right) => Date.parse(left.submittedAt) - Date.parse(right.submittedAt)),
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
        throw new Error("Journal response has an invalid note document version");
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
  const moduleInstanceId = value.moduleInstanceId;
  const moduleInstanceVersion = value.moduleInstanceVersion;
  if ((moduleInstanceId === null || moduleInstanceId === undefined)
      !== (moduleInstanceVersion === null || moduleInstanceVersion === undefined)) {
    throw new Error("Journal response has incomplete note module identity");
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
    followUps: followUps(value.followUps),
    version: typeof value.version === "number" ? value.version : 1,
    ...(moduleInstanceId === null || moduleInstanceId === undefined
      ? {}
      : {
          moduleInstanceId: asWidgetInstanceId(
            string(moduleInstanceId, "note module instance ID"),
          ),
          moduleInstanceVersion: positiveInteger(
            moduleInstanceVersion,
            "note module instance version",
          ),
        }),
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
    ...(Array.isArray(value.tombstones)
      ? { tombstones: value.tombstones.map(runningNote) }
      : {}),
  };
}

const nonNegativeInteger = (value: unknown, label: string): number => {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new Error(`Journal response has invalid ${label}`);
  }
  return value;
};

const positiveInteger = (value: unknown, label: string): number => {
  const result = nonNegativeInteger(value, label);
  if (result < 1) throw new Error(`Journal response has invalid ${label}`);
  return result;
};

const FIELD_VALUE_KINDS = new Set<JournalFieldValueKind>([
  "short_text", "long_text", "number", "scale", "boolean", "single_select",
  "multi_select", "local_time", "instant", "date", "duration", "reference",
]);

const nullableString = (value: unknown, label: string): string | null => {
  if (value === null) return null;
  return string(value, label);
};

const documentModuleState = (value: unknown): JournalDocumentModuleState => {
  if (!isRecord(value)) throw new Error("Journal response has invalid document section state");
  if (value.truthEligibility !== "allowed" || value.truthStartsDisabled !== true) {
    throw new Error("Journal response has invalid document Truth policy");
  }
  const role = string(value.role, "document role");
  if (value.state === "available") {
    return {
      state: "available",
      role,
      truthEligibility: "allowed",
      truthStartsDisabled: true,
    };
  }
  if (value.state !== "current") {
    throw new Error("Journal response has invalid document section lifecycle");
  }
  const href = string(value.href, "document Co-work link");
  if (!href.startsWith("/app/cowork?")) {
    throw new Error("Journal response has an unsafe document Co-work link");
  }
  if (typeof value.canOpenFull !== "boolean") {
    throw new Error("Journal response has invalid document open entitlement");
  }
  return {
    state: "current",
    role,
    truthEligibility: "allowed",
    truthStartsDisabled: true,
    href,
    storeId: string(value.storeId, "document store ID"),
    documentId: string(value.documentId, "document ID"),
    bindingId: string(value.bindingId, "document binding ID"),
    domainEntityId: string(value.domainEntityId, "document domain entity ID"),
    contentAuthorityEpoch: positiveInteger(
      value.contentAuthorityEpoch,
      "document authority epoch",
    ),
    canOpenFull: value.canOpenFull,
  };
};

function effectiveComposition(value: unknown): JournalEffectiveComposition | undefined {
  if (value === undefined || value === null) return undefined;
  if (!isRecord(value) || value.schemaVersion !== 1 || !isRecord(value.profile)
      || !Array.isArray(value.modules)) {
    throw new Error("Journal response has invalid effective composition");
  }
  const authorityState = value.authorityState;
  if (authorityState !== "legacy_compatibility"
      && authorityState !== "database_only"
      && authorityState !== "recovery_fenced") {
    throw new Error("Journal response has invalid composition authority");
  }
  const modules = value.modules.map((candidate): JournalEffectiveModule => {
    if (!isRecord(candidate) || !isRecord(candidate.settings)
        || !Array.isArray(candidate.fields)) {
      throw new Error("Journal response has invalid composition module");
    }
    const semanticMembership = candidate.semanticMembership;
    if (semanticMembership !== "included" && semanticMembership !== "excluded_by_schedule"
        && semanticMembership !== "unavailable") {
      throw new Error("Journal response has invalid module membership");
    }
    const behaviorId = candidate.behaviorId === undefined
      ? null
      : nullableString(candidate.behaviorId, "module behavior ID");
    const behaviorVersion = candidate.behaviorVersion === undefined
      ? null
      : candidate.behaviorVersion;
    if (behaviorVersion !== null && (typeof behaviorVersion !== "number"
        || !Number.isSafeInteger(behaviorVersion) || behaviorVersion < 1)) {
      throw new Error("Journal response has invalid module behavior version");
    }
    const aiContribution = candidate.aiContribution ?? "forbidden";
    if (aiContribution !== "forbidden" && aiContribution !== "allowed"
        && aiContribution !== "suggestion_only") {
      throw new Error("Journal response has invalid module AI contribution policy");
    }
    const document = candidate.document === undefined
      ? undefined
      : documentModuleState(candidate.document);
    if ((candidate.moduleTypeId === "document") !== (document !== undefined)) {
      throw new Error("Journal response has incomplete document module state");
    }
    return {
      slotId: string(candidate.slotId, "composition slot ID"),
      ordinal: nonNegativeInteger(candidate.ordinal, "module ordinal"),
      moduleInstanceId: string(candidate.moduleInstanceId, "module instance ID"),
      moduleInstanceVersion: positiveInteger(candidate.moduleInstanceVersion, "module instance version"),
      moduleTypeId: string(candidate.moduleTypeId, "module type ID"),
      moduleTypeVersion: positiveInteger(candidate.moduleTypeVersion, "module type version"),
      label: string(candidate.label, "module label"),
      behaviorId,
      behaviorVersion,
      aiContribution,
      semanticMembership,
      settings: candidate.settings,
      scheduleKind: string(candidate.scheduleKind, "module schedule"),
      scheduleEvidence: candidate.scheduleEvidence,
      ...(document === undefined ? {} : { document }),
      fields: candidate.fields.map((field) => {
        if (!isRecord(field)) throw new Error("Journal response has invalid composition field");
        const promptId = field.promptId;
        const promptVersion = field.promptVersion;
        if (promptId !== null && typeof promptId !== "string") {
          throw new Error("Journal response has invalid prompt ID");
        }
        if (promptVersion !== null && (typeof promptVersion !== "number"
            || !Number.isSafeInteger(promptVersion) || promptVersion < 1)) {
          throw new Error("Journal response has invalid prompt version");
        }
        const valueKind = field.valueKind;
        if (valueKind !== undefined && (typeof valueKind !== "string"
            || !FIELD_VALUE_KINDS.has(valueKind as JournalFieldValueKind))) {
          throw new Error("Journal response has invalid composition field value kind");
        }
        if (field.constraints !== undefined && !isRecord(field.constraints)) {
          throw new Error("Journal response has invalid composition field constraints");
        }
        const functionId = field.functionId === undefined
          ? undefined
          : nullableString(field.functionId, "field function ID");
        const functionVersion = field.functionVersion;
        if (functionVersion !== undefined && functionVersion !== null
            && (typeof functionVersion !== "number"
              || !Number.isSafeInteger(functionVersion) || functionVersion < 1)) {
          throw new Error("Journal response has invalid field function version");
        }
        if ((functionId === null) !== (functionVersion === null)
            || (functionId === undefined) !== (functionVersion === undefined)) {
          throw new Error("Journal response has incomplete field function binding");
        }
        return {
          compositionSlotId: string(field.compositionSlotId, "field composition slot ID"),
          ordinal: nonNegativeInteger(field.ordinal, "field ordinal"),
          fieldId: string(field.fieldId, "field ID"),
          fieldDefinitionVersion: positiveInteger(field.fieldDefinitionVersion, "field definition version"),
          ...(field.label === undefined ? {} : { label: string(field.label, "field label") }),
          ...(field.description === undefined
            ? {}
            : { description: string(field.description, "field description") }),
          ...(valueKind === undefined ? {} : { valueKind: valueKind as JournalFieldValueKind }),
          ...(field.unit === undefined ? {} : { unit: nullableString(field.unit, "field unit") }),
          ...(functionId === undefined ? {} : { functionId }),
          ...(functionVersion === undefined ? {} : { functionVersion }),
          ...(field.constraints === undefined ? {} : { constraints: field.constraints }),
          ...(field.behaviorId === undefined
            ? {}
            : { behaviorId: string(field.behaviorId, "field behavior ID") }),
          ...(field.behaviorVersion === undefined
            ? {}
            : { behaviorVersion: positiveInteger(field.behaviorVersion, "field behavior version") }),
          ...(field.privacyClass === undefined
            ? {}
            : { privacyClass: string(field.privacyClass, "field privacy class") }),
          ...(field.searchMode === undefined
            ? {}
            : { searchMode: string(field.searchMode, "field search mode") }),
          promptId,
          promptVersion,
          ...(field.promptWording === undefined
            ? {}
            : { promptWording: nullableString(field.promptWording, "field prompt wording") }),
          ...(field.promptHelp === undefined
            ? {}
            : { promptHelp: nullableString(field.promptHelp, "field prompt help") }),
          ...(field.promptRequiredness === undefined
            ? {}
            : { promptRequiredness: nullableString(field.promptRequiredness, "field prompt requiredness") }),
        };
      }),
    };
  });
  const profile = value.profile;
  const snapshotId = value.snapshotId;
  const snapshotVersion = value.snapshotVersion;
  if (snapshotId !== null && typeof snapshotId !== "string") {
    throw new Error("Journal response has invalid composition snapshot ID");
  }
  if (snapshotVersion !== null && (typeof snapshotVersion !== "number"
      || !Number.isSafeInteger(snapshotVersion) || snapshotVersion < 1)) {
    throw new Error("Journal response has invalid composition snapshot version");
  }
  return {
    schemaVersion: 1,
    persisted: value.persisted === true,
    snapshotId,
    snapshotVersion,
    compositionDigest: string(value.compositionDigest, "composition digest"),
    searchRecipeVersion: positiveInteger(value.searchRecipeVersion, "search recipe version"),
    activationRevision: positiveInteger(value.activationRevision, "profile activation revision"),
    authorityState,
    profile: {
      profileId: string(profile.profileId, "profile ID"),
      profileRevision: positiveInteger(profile.profileRevision, "profile revision"),
      formatVersion: positiveInteger(profile.formatVersion, "profile format version"),
      name: string(profile.name, "profile name"),
      description: string(profile.description, "profile description"),
      profileDigest: string(profile.profileDigest, "profile digest"),
    },
    modules,
  };
}

function logEntry(value: unknown): NativeJournalLogEntry {
  if (!isRecord(value) || value.itemKind !== "record") {
    throw new Error("Journal response has invalid log entry");
  }
  const moduleInstanceId = value.moduleInstanceId;
  const moduleInstanceVersion = value.moduleInstanceVersion;
  if ((moduleInstanceId === null || moduleInstanceId === undefined)
      !== (moduleInstanceVersion === null || moduleInstanceVersion === undefined)) {
    throw new Error("Journal response has incomplete log module identity");
  }
  return {
    itemId: string(value.itemId, "log entry ID"),
    text: string(value.text, "log entry text"),
    markdown: string(value.markdown, "log entry Markdown"),
    createdAt: string(value.createdAt, "log entry creation time"),
    updatedAt: string(value.updatedAt, "log entry update time"),
    revision: positiveInteger(value.revision, "log entry revision"),
    lifecycle: string(value.lifecycle, "log entry lifecycle"),
    authorityKind: string(value.authorityKind, "log entry authority"),
    ...(optionalString(value.sourceRef) ? { sourceRef: optionalString(value.sourceRef) } : {}),
    ...(moduleInstanceId === null || moduleInstanceId === undefined
      ? {}
      : {
          moduleInstanceId: string(moduleInstanceId, "log module instance ID"),
          moduleInstanceVersion: positiveInteger(moduleInstanceVersion, "log module instance version"),
        }),
  };
}

function fieldValue(value: unknown): NativeJournalFieldValue {
  if (!isRecord(value) || typeof value.valueKind !== "string"
      || !FIELD_VALUE_KINDS.has(value.valueKind as JournalFieldValueKind)) {
    throw new Error("Journal response has invalid field value");
  }
  const rawValue = value.value;
  const referenceValue = Array.isArray(rawValue) && rawValue.every(
    (item) => isRecord(item) && typeof item.kind === "string"
      && typeof item.id === "string"
      && (item.revision === undefined || typeof item.revision === "string"),
  );
  if (rawValue !== null && typeof rawValue !== "string" && typeof rawValue !== "number"
      && typeof rawValue !== "boolean" && (!Array.isArray(rawValue)
        || (!rawValue.every((item) => typeof item === "string") && !referenceValue))) {
    throw new Error("Journal response has invalid typed field value");
  }
  const disposition = value.disposition;
  if (disposition !== null && disposition !== undefined && disposition !== "missing"
      && disposition !== "skipped" && disposition !== "declined") {
    throw new Error("Journal response has invalid field disposition");
  }
  return {
    valueId: string(value.valueId, "field value ID"),
    moduleInstanceId: string(value.moduleInstanceId, "field module instance ID"),
    moduleInstanceVersion: positiveInteger(value.moduleInstanceVersion, "field module instance version"),
    fieldId: string(value.fieldId, "field ID"),
    fieldDefinitionVersion: positiveInteger(value.fieldDefinitionVersion, "field definition version"),
    valueKind: value.valueKind as JournalFieldValueKind,
    ...(disposition === null || disposition === undefined ? {} : { disposition }),
    value: rawValue as JournalFieldValue | readonly JournalFieldReference[],
    currentRevision: positiveInteger(value.currentRevision, "field value revision"),
    authorship: string(value.authorship, "field value authorship"),
    reviewState: string(value.reviewState, "field value review state"),
    ...(optionalString(value.sourceRef) ? { sourceRef: optionalString(value.sourceRef) } : {}),
    lifecycle: string(value.lifecycle, "field value lifecycle"),
  };
}

function nativeModuleItem(value: unknown): NativeJournalModuleItem {
  if (!isRecord(value)) throw new Error("Journal response has invalid native item");
  const moduleInstanceId = value.moduleInstanceId;
  const moduleInstanceVersion = value.moduleInstanceVersion;
  if ((moduleInstanceId === null || moduleInstanceId === undefined)
      !== (moduleInstanceVersion === null || moduleInstanceVersion === undefined)) {
    throw new Error("Journal response has incomplete native-item module identity");
  }
  return {
    itemId: string(value.itemId, "native item ID"),
    itemKind: string(value.itemKind, "native item kind"),
    text: string(value.text, "native item text"),
    createdAt: string(value.createdAt, "native item created time"),
    updatedAt: string(value.updatedAt, "native item updated time"),
    revision: positiveInteger(value.revision, "native item revision"),
    lifecycle: string(value.lifecycle, "native item lifecycle"),
    authorityKind: string(value.authorityKind, "native item authority"),
    actions: Array.isArray(value.actions)
      ? strings(value.actions) as readonly JournalItemAction[]
      : [],
    relations: Array.isArray(value.relations)
      ? value.relations.map((relation): JournalItemRelation => {
          if (!isRecord(relation)) throw new Error("Journal response has invalid item relation");
          return {
            relationId: string(relation.relationId, "relation ID"),
            relationKind: string(relation.relationKind, "relation kind"),
            targetDomain: string(relation.targetDomain, "relation target domain"),
            targetId: string(relation.targetId, "relation target ID"),
            ...(optionalString(relation.targetRevision)
              ? { targetRevision: optionalString(relation.targetRevision) }
              : {}),
            lifecycle: string(relation.lifecycle, "relation lifecycle"),
            revision: positiveInteger(relation.revision, "relation revision"),
          };
        })
      : [],
    ...(optionalString(value.sourceRef) ? { sourceRef: optionalString(value.sourceRef) } : {}),
    ...(moduleInstanceId === null || moduleInstanceId === undefined
      ? {}
      : {
          moduleInstanceId: string(moduleInstanceId, "native item module instance ID"),
          moduleInstanceVersion: positiveInteger(
            moduleInstanceVersion,
            "native item module instance version",
          ),
        }),
  };
}

function promptGeneration(value: unknown): JournalPromptGenerationRequest {
  if (!isRecord(value)) throw new Error("Journal response has invalid prompt generation");
  const status = value.status;
  if (status !== "pending" && status !== "leased" && status !== "succeeded"
      && status !== "failed" && status !== "canceled" && status !== "expired") {
    throw new Error("Journal response has invalid prompt generation status");
  }
  return {
    requestId: string(value.requestId, "prompt generation request ID"),
    status,
    retryable: value.retryable === true,
    attempts: nonNegativeInteger(value.attempts, "prompt generation attempts"),
    ...(optionalString(value.providerId) ? { providerId: optionalString(value.providerId) } : {}),
    ...(optionalString(value.modelId) ? { modelId: optionalString(value.modelId) } : {}),
    ...(optionalString(value.errorCode) ? { errorCode: optionalString(value.errorCode) } : {}),
    createdAt: string(value.createdAt, "prompt generation created time"),
    updatedAt: string(value.updatedAt, "prompt generation updated time"),
    ...(optionalString(value.completedAt) ? { completedAt: optionalString(value.completedAt) } : {}),
  };
}

function promptVariant(value: unknown): JournalPromptResultVariant {
  if (!isRecord(value)) throw new Error("Journal response has invalid prompt result");
  return {
    variantId: string(value.variantId, "prompt result variant ID"),
    resultText: string(value.resultText, "prompt result text"),
    ...(optionalString(value.sourceRef) ? { sourceRef: optionalString(value.sourceRef) } : {}),
    authorship: string(value.authorship, "prompt result authorship"),
    reviewState: string(value.reviewState, "prompt result review state"),
    lifecycle: string(value.lifecycle, "prompt result lifecycle"),
    producerId: string(value.producerId, "prompt result producer"),
    ...(optionalString(value.providerId) ? { providerId: optionalString(value.providerId) } : {}),
    ...(optionalString(value.modelId) ? { modelId: optionalString(value.modelId) } : {}),
    createdAt: string(value.createdAt, "prompt result created time"),
  };
}

function promptInteraction(value: unknown): JournalPromptInteraction {
  if (!isRecord(value) || !Array.isArray(value.variants)
      || !Array.isArray(value.generationRequests)) {
    throw new Error("Journal response has invalid prompt interaction");
  }
  return {
    interactionId: string(value.interactionId, "prompt interaction ID"),
    moduleInstanceId: string(value.moduleInstanceId, "prompt module instance ID"),
    moduleInstanceVersion: positiveInteger(value.moduleInstanceVersion, "prompt module version"),
    promptId: string(value.promptId, "prompt ID"),
    promptVersion: positiveInteger(value.promptVersion, "prompt version"),
    promptWording: string(value.promptWording, "prompt wording"),
    ...(optionalString(value.promptHelp) ? { promptHelp: optionalString(value.promptHelp) } : {}),
    inputText: string(value.inputText, "prompt seed"),
    ...(optionalString(value.inputSourceRef) ? { inputSourceRef: optionalString(value.inputSourceRef) } : {}),
    lifecycle: string(value.lifecycle, "prompt interaction lifecycle"),
    currentRevision: positiveInteger(value.currentRevision, "prompt interaction revision"),
    variants: value.variants.map(promptVariant),
    generationRequests: value.generationRequests.map(promptGeneration),
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
    effectiveComposition: effectiveComposition(
      view.effectiveComposition ?? view.effective_composition,
    ),
    logEntries: Array.isArray(view.logEntries)
      ? view.logEntries.map(logEntry)
      : [],
    fieldValues: Array.isArray(view.fieldValues)
      ? view.fieldValues.map(fieldValue)
      : [],
    nativeItems: Array.isArray(view.nativeItems)
      ? view.nativeItems.map(nativeModuleItem)
      : [],
    promptInteractions: Array.isArray(view.promptInteractions)
      ? view.promptInteractions.map(promptInteraction)
      : [],
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
  viewAccess: JournalViewModel["access"],
): JournalTimelineInput {
  const candidate = legacy.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.timeline];
  if (candidate === undefined || candidate.day.dayId !== native.day.dayId) {
    return emptyTimeline(native);
  }
  const timelineAccess = candidate.access ?? {
    mode: "read_only" as const,
    reason: "This timeline is available for review only.",
  };
  const viewExplainsTimelineAccess =
    viewAccess.mode === "read_only" &&
    timelineAccess.mode === "read_only" &&
    viewAccess.reason === timelineAccess.reason;
  return {
    ...candidate,
    revision: native.revision,
    day: native.day,
    access: timelineAccess,
    // Repeat the timeline's review-only notice only when it conveys a
    // limitation beyond the access notice already owned by Journal chrome.
    accessNotice: viewExplainsTimelineAccess ? "view" : "widget",
  } as JournalTimelineInput;
}

function timelineFromNative(
  native: NativeJournalPayload,
  viewAccess: JournalViewModel["access"],
  module?: JournalEffectiveModule,
): JournalTimelineInput {
  const records = native.logEntries
    .filter((entry) => entry.lifecycle !== "deleted")
    .filter((entry) => module === undefined
      || (entry.moduleInstanceId === module.moduleInstanceId
        && entry.moduleInstanceVersion === module.moduleInstanceVersion));
  const recordIds = new Set(records.map((entry) => entry.itemId));
  const moduleItems = native.nativeItems
    .filter((item) => item.itemKind !== "running_note" && !recordIds.has(item.itemId)
      && item.lifecycle !== "deleted" && item.lifecycle !== "tombstoned")
    .filter((item) => module === undefined
      || (item.moduleInstanceId === module.moduleInstanceId
        && item.moduleInstanceVersion === module.moduleInstanceVersion));
  return {
    instanceId: JOURNAL_WIDGET_INSTANCE_IDS.timeline,
    revision: native.revision,
    day: native.day,
    access: viewAccess,
    accessNotice: "view",
    renderMode: "timeline",
    density: "comfortable",
    items: [
      ...records.map((entry) => {
        const lines = entry.text.split(/\r?\n/u);
        const title = lines.find((line) => line.trim().length > 0)?.trim()
          ?? "Journal entry";
        const detail = lines.slice(1).join("\n").trim();
        return {
          itemId: entry.itemId,
          kind: "record" as const,
          shape: "point" as const,
          at: entry.createdAt,
          title,
          ...(detail.length === 0 ? {} : { detail }),
          status: "observed" as const,
          mutability: "past_protected" as const,
          precision: "exact" as const,
          provenance: {
            source: "local_submission" as const,
            label: entry.authorityKind,
          },
          navigation: {
            targetType: "journal_item" as const,
            targetId: entry.itemId,
          },
        };
      }),
      ...moduleItems.map((item) => {
        const lines = item.text.split(/\r?\n/u);
        const title = lines.find((line) => line.trim().length > 0)?.trim()
          ?? item.itemKind.replace(/_/gu, " ");
        const detail = lines.slice(1).join("\n").trim();
        return {
          itemId: item.itemId,
          kind: "record" as const,
          shape: "point" as const,
          at: item.createdAt,
          title,
          ...(detail.length === 0 ? {} : { detail }),
          status: "observed" as const,
          mutability: "past_protected" as const,
          precision: "exact" as const,
          provenance: {
            source: "local_submission" as const,
            label: item.authorityKind,
          },
          navigation: {
            targetType: "journal_item" as const,
            targetId: item.itemId,
          },
        };
      }),
    ],
  };
}

const moduleDescription = (module: JournalEffectiveModule): string | undefined =>
  typeof module.settings.description === "string"
    ? module.settings.description
    : undefined;

const fieldPresentation = (
  module: JournalEffectiveModule,
  fieldId: string,
): Record<string, unknown> => {
  const fields = module.settings.fields;
  if (!Array.isArray(fields)) return {};
  return fields.find(
    (candidate): candidate is Record<string, unknown> =>
      isRecord(candidate) && candidate.fieldId === fieldId,
  ) ?? {};
};

function genericModuleInput(
  module: JournalEffectiveModule,
  native: NativeJournalPayload,
  viewAccess: JournalViewModel["access"],
): JournalGenericModuleInput {
  const values = native.fieldValues.filter(
    (value) =>
      value.moduleInstanceId === module.moduleInstanceId &&
      value.moduleInstanceVersion === module.moduleInstanceVersion &&
      value.lifecycle !== "deleted",
  );
  const items = native.nativeItems.filter(
    (item) =>
      item.moduleInstanceId === module.moduleInstanceId &&
      item.moduleInstanceVersion === module.moduleInstanceVersion &&
      item.lifecycle !== "deleted" && item.lifecycle !== "tombstoned",
  );
  const promptInteractions = native.promptInteractions.filter(
    (interaction) => interaction.moduleInstanceId === module.moduleInstanceId
      && interaction.moduleInstanceVersion === module.moduleInstanceVersion,
  );
  const valueByField = new Map<string, NativeJournalFieldValue>();
  for (const value of values) {
    const prior = valueByField.get(value.fieldId);
    if (prior === undefined || value.currentRevision > prior.currentRevision) {
      valueByField.set(value.fieldId, value);
    }
  }
  const references: JournalEffectiveModule["fields"] = module.fields.length > 0
    ? [...module.fields].sort((left, right) => left.ordinal - right.ordinal)
    : values.map((value, ordinal) => ({
        compositionSlotId: `${module.slotId}:field:${value.fieldId}`,
        ordinal,
        fieldId: value.fieldId,
        fieldDefinitionVersion: value.fieldDefinitionVersion,
        promptId: null,
        promptVersion: null,
      }));
  const fields: readonly JournalEffectiveFieldInput[] = references.map((reference) => {
    const current = valueByField.get(reference.fieldId);
    const presentation = fieldPresentation(module, reference.fieldId);
    const constraints = reference.constraints
      ?? (isRecord(presentation.constraints) ? presentation.constraints : presentation);
    const rawOptions = constraints.options ?? presentation.options;
    const options = Array.isArray(rawOptions)
      ? rawOptions.flatMap((option) =>
          typeof option === "string"
            ? [{ value: option, label: option.replace(/_/gu, " ") }]
            : isRecord(option) && typeof option.value === "string" && typeof option.label === "string"
              ? [{ value: option.value, label: option.label }]
              : [],
        )
      : undefined;
    const label = reference.promptWording
      ?? reference.label
      ?? (typeof presentation.label === "string" ? presentation.label : reference.fieldId);
    const description = reference.promptHelp
      ?? reference.description
      ?? (typeof presentation.description === "string" ? presentation.description : undefined);
    const unit = reference.unit === undefined
      ? (typeof presentation.unit === "string" ? presentation.unit : undefined)
      : reference.unit ?? undefined;
    return {
      ...(current?.valueId === undefined ? {} : { valueId: current.valueId }),
      compositionSlotId: reference.compositionSlotId,
      fieldId: reference.fieldId,
      definitionVersion: reference.fieldDefinitionVersion,
      ...(reference.promptId === null ? {} : { promptId: reference.promptId }),
      ...(reference.promptVersion === null ? {} : { promptVersion: reference.promptVersion }),
      label,
      ...(description === undefined ? {} : { description }),
      valueKind: reference.valueKind ?? current?.valueKind ?? "short_text",
      value: current?.value ?? null,
      ...(current?.currentRevision === undefined ? {} : { valueRevision: current.currentRevision }),
      required: reference.promptRequiredness === "required" || presentation.required === true,
      ...(unit === undefined ? {} : { unit }),
      ...(reference.functionId === null || reference.functionId === undefined
        ? {}
        : { functionId: reference.functionId }),
      ...(reference.functionVersion === null || reference.functionVersion === undefined
        ? {}
        : { functionVersion: reference.functionVersion }),
      ...(typeof constraints.minimum === "number" ? { minimum: constraints.minimum } : {}),
      ...(typeof constraints.maximum === "number" ? { maximum: constraints.maximum } : {}),
      ...(options === undefined ? {} : { options }),
      ...(current?.disposition === undefined ? {} : { disposition: current.disposition }),
      readOnly: viewAccess.mode === "read_only"
        || module.semanticMembership !== "included",
      ...(current?.authorship === undefined ? {} : { authorship: current.authorship }),
      ...(current?.reviewState === undefined ? {} : { reviewState: current.reviewState }),
      ...(current?.sourceRef === undefined ? {} : { sourceRef: current.sourceRef }),
    };
  });
  return {
    instanceId: module.moduleInstanceId,
    revision: native.revision,
    dayId: native.day.dayId,
    localDate: native.day.localDate,
    access: viewAccess,
    moduleTypeId: module.moduleTypeId,
    moduleInstanceVersion: module.moduleInstanceVersion,
    moduleDefinitionVersion: module.moduleTypeVersion,
    behaviorId: module.behaviorId,
    behaviorVersion: module.behaviorVersion,
    aiContribution: module.aiContribution,
    label: module.label,
    ...(moduleDescription(module) === undefined ? {} : { description: moduleDescription(module) }),
    fields,
    items,
    promptInteractions,
    ...(module.document === undefined ? {} : { document: module.document }),
    ...(module.semanticMembership === "unavailable"
      ? { unavailableReason: "This configured Journal section is unavailable, but its schema and saved values remain visible." }
      : {}),
  };
}

const moduleBinding = (module: JournalEffectiveModule) => {
  if (module.moduleTypeId === "capture") {
    return { role: JOURNAL_ROLE_IDS.capture, widget: JOURNAL_WIDGET_TYPE_BY_INSTANCE.get(JOURNAL_INSTANCE_IDS.capture)! };
  }
  if (module.moduleTypeId === "day_stream") {
    return { role: JOURNAL_ROLE_IDS.timeline, widget: JOURNAL_WIDGET_TYPE_BY_INSTANCE.get(JOURNAL_INSTANCE_IDS.timeline)! };
  }
  if (module.moduleTypeId === "record_collection") {
    return { role: JOURNAL_ROLE_IDS.runningNotes, widget: JOURNAL_WIDGET_TYPE_BY_INSTANCE.get(JOURNAL_INSTANCE_IDS.runningNotes)! };
  }
  return { role: JOURNAL_GENERIC_ROLE_ID, widget: JOURNAL_GENERIC_WIDGET_TYPE_ID };
};

function providerComposition(
  native: NativeJournalPayload,
  viewAccess: JournalViewModel["access"],
  timeline: JournalTimelineInput,
  databaseAuthority: boolean,
): {
  readonly composition: EffectiveViewComposition;
  readonly widgetInputs: Readonly<Record<string, JournalWidgetInput>>;
} | undefined {
  const source = native.effectiveComposition;
  if (source === undefined) return undefined;
  const modules = [...source.modules]
    .filter((module) => module.semanticMembership !== "excluded_by_schedule")
    .sort((left, right) => left.ordinal - right.ordinal);
  const simple = modules.length === 3
    && modules.some((module) => module.moduleTypeId === "capture")
    && modules.some((module) => module.moduleTypeId === "day_stream")
    && modules.some((module) => module.moduleTypeId === "record_collection");
  const simpleLayout = (module: JournalEffectiveModule) =>
    module.moduleTypeId === "capture"
      ? { x: 0, y: 0, w: 8, h: 14 }
      : module.moduleTypeId === "record_collection"
        ? { x: 0, y: 14, w: 8, h: 6 }
        : { x: 8, y: 0, w: 16, h: 16 };
  const slots: DefaultWidgetSlot[] = modules.map((module, index) => {
    const binding = moduleBinding(module);
    return {
      slotId: asWidgetSlotId(module.slotId),
      defaultInstanceId: asWidgetInstanceId(module.moduleInstanceId),
      requiredRole: binding.role,
      defaultWidgetTypeId: binding.widget,
      presence: "default_on",
      help: {
        summary: module.label,
        details: moduleDescription(module)
          ?? "This section comes from the immutable Journal composition for this day.",
      },
      defaultSettings: module.settings as JsonValue,
      defaultBindings: {
        moduleTypeId: module.moduleTypeId,
        moduleInstanceVersion: module.moduleInstanceVersion,
        compositionDigest: source.compositionDigest,
      },
      defaultLayout: simple ? simpleLayout(module) : { x: 0, y: index * 8, w: 24, h: 8 },
      allowedSubstitution: { minimumDefinitionVersion: 1 },
      semanticComposition: "provider_owned",
    };
  });
  const inputs: Record<string, JournalWidgetInput> = {};
  for (const module of modules) {
    if (module.moduleTypeId === "capture") {
      inputs[module.moduleInstanceId] = {
        ...native.capture,
        instanceId: module.moduleInstanceId,
        revision: native.revision,
        access: viewAccess,
        accessNotice: "view",
      };
    } else if (module.moduleTypeId === "day_stream") {
      inputs[module.moduleInstanceId] = {
        ...(databaseAuthority
          ? timelineFromNative(native, viewAccess, module)
          : timeline),
        instanceId: module.moduleInstanceId,
      };
    } else if (module.moduleTypeId === "record_collection") {
      const items = databaseAuthority
        ? native.runningNotes.items.filter((item) =>
            item.moduleInstanceId === module.moduleInstanceId
            && item.moduleInstanceVersion === module.moduleInstanceVersion,
          )
        : native.runningNotes.items;
      const supplementalItems = databaseAuthority
        ? native.nativeItems.filter((item) =>
            item.itemKind !== "running_note"
            && item.lifecycle !== "deleted"
            && item.lifecycle !== "tombstoned"
            && item.moduleInstanceId === module.moduleInstanceId
            && item.moduleInstanceVersion === module.moduleInstanceVersion,
          )
        : [];
      inputs[module.moduleInstanceId] = {
        ...native.runningNotes,
        instanceId: module.moduleInstanceId,
        revision: native.revision,
        items,
        ...(databaseAuthority && native.runningNotes.tombstones !== undefined
          ? {
              tombstones: native.runningNotes.tombstones.filter((item) =>
                item.moduleInstanceId === module.moduleInstanceId
                && item.moduleInstanceVersion === module.moduleInstanceVersion,
              ),
            }
          : {}),
        ...(supplementalItems.length === 0 ? {} : { supplementalItems }),
        access:
          viewAccess.mode === "read_only"
            ? viewAccess
            : native.runningNotes.access,
      };
    } else {
      inputs[module.moduleInstanceId] = genericModuleInput(
        module,
        native,
        databaseAuthority
          ? viewAccess
          : {
              mode: "read_only",
              reason: "Typed fields become editable after Journal database cutover.",
            },
      );
    }
  }
  const order = slots.map((slot) => slot.slotId);
  return {
    composition: {
      compositionId: source.snapshotId ?? `projected:${source.compositionDigest}`,
      revision: `${source.activationRevision}:${source.compositionDigest}`,
      defaultSlots: slots,
      readingOrder: order,
      mobileOrder: order,
    },
    widgetInputs: inputs,
  };
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
  readonly follow_up_action?: string;
  readonly smart_disclosure_sha256?: string;
}): Promise<string> {
  const exactTextSha256 = await sha256Hex(payload.exact_text);
  const canonical = JSON.stringify({
    client_mutation_id: payload.client_mutation_id,
    day_id: payload.day_id,
    exact_text_sha256: exactTextSha256,
    ...(payload.follow_up_action ? { follow_up_action: payload.follow_up_action } : {}),
    input_mode: payload.input_mode,
    mode: payload.mode,
    schema: "wb.journal-capture-gesture/v1",
    ...(payload.smart_disclosure_sha256 ? { smart_disclosure_sha256: payload.smart_disclosure_sha256 } : {}),
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
  readonly #invalidationListeners = new Set<(invalidation: AppInvalidation) => void>();
  #generationPollTimer: ReturnType<typeof setTimeout> | undefined;
  #generationPollAttempt = 0;
  #generationPending = false;

  constructor(options: HttpJournalProviderOptions = {}) {
    this.#fetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.#legacy = options.legacyProvider ?? new LegacyFlaskViewAdapter({ fetchImpl: this.#fetch });
    this.#clock = options.clock ?? (() => new Date().toISOString());
    this.#navigate = options.navigate ?? ((href) => window.location.assign(href));
  }

  subscribeInvalidations(listener: (invalidation: AppInvalidation) => void): () => void {
    this.#invalidationListeners.add(listener);
    if (this.#generationPending) this.#scheduleGenerationPoll();
    return () => {
      this.#invalidationListeners.delete(listener);
      if (this.#invalidationListeners.size === 0 && this.#generationPollTimer !== undefined) {
        clearTimeout(this.#generationPollTimer);
        this.#generationPollTimer = undefined;
      }
    };
  }

  async loadView(viewId: ViewId, _request: ViewLoadRequest): Promise<HttpJournalViewSnapshot> {
    if (viewId !== JOURNAL_VIEW_DEFINITION_ID) {
      throw new Error(`HttpJournalProvider cannot load view ${viewId}`);
    }
    const identity = await initializeLocalIdentity({ fetchImpl: this.#fetch });
    const native = await this.#readNative();
    const authorityState = native.effectiveComposition?.authorityState
      ?? "legacy_compatibility";
    const databaseAuthority = authorityState === "database_only"
      || authorityState === "recovery_fenced";
    const recoveryFenced = authorityState === "recovery_fenced";
    // The old Today adapter is consulted only while compatibility is the
    // explicit authority. Recovery is database-backed and fail-closed.
    const legacy = databaseAuthority
      ? null
      : await this.#legacy.loadView(viewId, { reason: "refresh" });
    const canWrite = identity.authenticated && native.access.mode === "read_write"
      && !recoveryFenced;
    const writeAccess = canWrite
      ? ({ mode: "read_write" } as const)
      : ({
          mode: "read_only",
          reason:
            recoveryFenced
              ? "Journal recovery is still reconciling. Editing is paused."
              : native.access.mode === "read_only"
              ? native.access.reason
              : "Editing is paused in this browser. Open Journal from the Work Buddy tray to reconnect.",
        } as const);
    const issues = [...native.quality.issues];
    if (recoveryFenced) {
      issues.push({
        code: "journal_recovery_fenced",
        message: "Journal recovery is still reconciling. Content is read-only.",
        affectedInstanceIds: native.effectiveComposition?.modules.map(
          (module) => module.moduleInstanceId,
        ) ?? [],
      });
    }
    if (legacy?.model === null) {
      issues.push({
        code: "legacy_today_unavailable",
        message: legacy.quality.message ?? "Live Today timeline data is unavailable.",
        affectedInstanceIds: [JOURNAL_WIDGET_INSTANCE_IDS.timeline],
      });
    }
    const timeline = databaseAuthority
      ? timelineFromNative(native, writeAccess)
      : timelineFromLegacy(legacy!, native, writeAccess);
    const dynamic = providerComposition(
      native,
      writeAccess,
      timeline,
      databaseAuthority,
    );
    const fixedWidgetInputs = {
      [JOURNAL_WIDGET_INSTANCE_IDS.capture]: {
        ...native.capture,
        revision: native.revision,
        access: writeAccess,
        accessNotice: "view" as const,
      },
      [JOURNAL_WIDGET_INSTANCE_IDS.timeline]: timeline,
      [JOURNAL_WIDGET_INSTANCE_IDS.runningNotes]: {
        ...native.runningNotes,
        revision: native.revision,
        access:
          writeAccess.mode === "read_only"
            ? writeAccess
            : native.runningNotes.access,
      },
    };
    const modelRevision = databaseAuthority
      ? `${native.revision}:${native.effectiveComposition!.compositionDigest}`
      : `${native.revision}:${legacy?.revision ?? "timeline-unavailable"}`;
    const model: JournalViewModel = {
      schemaVersion: 1,
      viewId: JOURNAL_VIEW_ID,
      revision: modelRevision,
      day: native.day,
      access: writeAccess,
      quality: { ...native.quality, issues },
      source: { kind: "live" },
      ...(native.effectiveComposition === undefined
        ? {}
        : { effectiveComposition: native.effectiveComposition }),
      widgetInputs: {
        ...fixedWidgetInputs,
        ...dynamic?.widgetInputs,
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
      ...(dynamic === undefined
        ? {}
        : { effectiveComposition: dynamic.composition }),
      model,
      bindings: bindings(model),
      widgetInputs,
    };
    this.#last = snapshot;
    this.#updateGenerationPolling(native.promptInteractions.some((interaction) =>
      interaction.generationRequests.some((generation) =>
        generation.status === "pending" || generation.status === "leased"),
    ));
    return snapshot;
  }

  #updateGenerationPolling(active: boolean): void {
    this.#generationPending = active;
    if (!active) {
      if (this.#generationPollTimer !== undefined) clearTimeout(this.#generationPollTimer);
      this.#generationPollTimer = undefined;
      this.#generationPollAttempt = 0;
      return;
    }
    this.#scheduleGenerationPoll();
  }

  #scheduleGenerationPoll(): void {
    if (!this.#generationPending || this.#generationPollTimer !== undefined
        || this.#invalidationListeners.size === 0 || this.#generationPollAttempt >= 24) return;
    const delay = Math.min(15_000, 1_000 * 2 ** Math.min(this.#generationPollAttempt, 4));
    this.#generationPollTimer = setTimeout(() => {
      this.#generationPollTimer = undefined;
      this.#generationPollAttempt += 1;
      const invalidation: AppInvalidation = {
        id: `journal-generation-poll:${this.#generationPollAttempt}:${Date.now()}`,
        appId: JOURNAL_APP_ID,
        viewIds: [JOURNAL_VIEW_DEFINITION_ID],
        reason: "journal_prompt_generation_poll",
        observedAt: this.#clock(),
      };
      for (const listener of this.#invalidationListeners) listener(invalidation);
      this.#scheduleGenerationPoll();
    }, delay);
  }

  async loadWidget(
    widgetTypeId: WidgetTypeId,
    request: WidgetLoadRequest,
  ): Promise<HttpJournalWidgetSnapshot> {
    if (request.viewId !== JOURNAL_VIEW_DEFINITION_ID) {
      throw new Error(`HttpJournalProvider cannot load widgets for ${request.viewId}`);
    }
    const snapshot =
      this.#last ??
      (await this.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" }));
    const dynamicSlot = snapshot.effectiveComposition?.defaultSlots.find(
      (slot) => slot.defaultInstanceId === request.instanceId,
    );
    const expected =
      dynamicSlot?.defaultWidgetTypeId ??
      JOURNAL_WIDGET_TYPE_BY_INSTANCE.get(request.instanceId);
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
    const input = snapshot.widgetInputs[request.instanceId];
    return {
      widgetTypeId,
      instanceId: request.instanceId,
      revision: snapshot.revision,
      observedAt: snapshot.observedAt,
      // Whole-view read-only access is already explained once in Journal
      // chrome. Other contextual states (stale/offline) remain widget-visible.
      status:
        input === undefined
          ? "unavailable"
          : snapshot.status === "read-only"
            ? "ready"
            : snapshot.status,
      quality: snapshot.quality,
      input: input ?? null,
    };
  }

  async dispatch(intent: DashboardIntent): Promise<IntentResult> {
    if (intent.view_id !== JOURNAL_VIEW_DEFINITION_ID) {
      return this.#result(intent, "rejected", "Intent targets a different view.");
    }
    const moduleType = this.#last?.model?.effectiveComposition?.modules.find(
      (module) => module.moduleInstanceId === intent.instance_id,
    )?.moduleTypeId;
    const captureInstance =
      intent.instance_id === JOURNAL_INSTANCE_IDS.capture || moduleType === "capture";
    const notesInstance =
      intent.instance_id === JOURNAL_INSTANCE_IDS.runningNotes ||
      moduleType === "record_collection";
    if (intent.intent_type === "wb.journal.field-value.put") {
      return this.#putFieldValue(intent);
    }
    if (intent.intent_type === "wb.journal.item-action") {
      return this.#actOnItem(intent);
    }
    if (notesInstance && intent.intent_type === "wb.notes.edit-requested") {
      return this.#actOnItem(intent, "edit");
    }
    if (notesInstance && intent.intent_type === "wb.notes.delete-requested") {
      return this.#actOnItem(intent, "tombstone");
    }
    if (notesInstance && intent.intent_type === "wb.notes.restore-requested") {
      return this.#actOnItem(intent, "restore");
    }
    if (intent.intent_type === "wb.journal.prompt-create") {
      return this.#createPromptInteraction(intent);
    }
    if (intent.intent_type === "wb.journal.prompt-generate") {
      return this.#generatePromptResult(intent);
    }
    if (intent.intent_type === "wb.journal.prompt-decision") {
      return this.#decidePromptResult(intent);
    }
    if (moduleType === "document" && intent.intent_type === "wb.journal.document.open") {
      return this.#openJournalDocumentModule(intent);
    }
    if (captureInstance) {
      if (intent.intent_type === "wb.capture.availability-refresh") {
        try {
          this.#last = undefined;
          await this.loadView(JOURNAL_VIEW_DEFINITION_ID, { reason: "refresh" });
          return this.#result(intent, "accepted", "Smart availability checked. No text was sent to a model.");
        } catch {
          return this.#result(intent, "unavailable", "Smart setup could not be checked. Direct capture is still available.");
        }
      }
      if (intent.intent_type === "wb.capture.retry-requested") return this.#retryCapture(intent);
    }
    if (
      intent.intent_type === "wb.notes.open-document-requested" &&
      notesInstance &&
      isRecord(intent.payload)
    ) {
      return this.#openRunningNoteDocument(intent);
    }
    if (
      intent.intent_type !== "wb.capture.submit" ||
      !captureInstance ||
      intent.client_mutation_id === undefined ||
      !isRecord(intent.payload)
    ) {
      return this.#result(
        intent,
        "unavailable",
        intent.intent_type.startsWith("wb.notes.")
          ? "Open this note in Co-work to make changes."
          : "That live Journal action is not available yet.",
      );
    }
    const exactText = intent.payload.exact_text;
    if (typeof exactText !== "string") {
      return this.#result(intent, "rejected", "Capture text is required.");
    }
    const disclosureSha256 = intent.payload.smart_disclosure_sha256;
    if ((intent.payload.mode === "smart" && disclosureSha256 === undefined)
        || (disclosureSha256 !== undefined && (typeof disclosureSha256 !== "string" || !/^[0-9a-f]{64}$/.test(disclosureSha256)))) {
      return this.#result(intent, "rejected", "Review the current Smart disclosure before capturing.");
    }
    const body = {
      client_mutation_id: intent.client_mutation_id,
      day_id: string(intent.payload.day_id, "capture day"),
      target_id: string(intent.payload.target_id, "capture destination"),
      mode: string(intent.payload.mode, "capture mode"),
      ...(typeof disclosureSha256 === "string" ? { smart_disclosure_sha256: disclosureSha256 } : {}),
      exact_text: exactText,
      input_mode: "unknown",
      ...(typeof intent.payload.follow_up_action === "string" ? { follow_up_action: intent.payload.follow_up_action } : {}),
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
          "Editing is paused in this browser. Open Journal from the Work Buddy tray to reconnect.",
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
      if (response.ok && isRecord(payload) && payload.ok === true
          && payload.queued === true && payload.persisted === true) {
        return this.#result(
          intent,
          "accepted",
          typeof payload.message === "string"
            ? payload.message
            : "Saved and queued while Journal maintenance finishes.",
        );
      }
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

  async #openJournalDocumentModule(intent: DashboardIntent): Promise<IntentResult> {
    if (this.#last?.model?.effectiveComposition?.authorityState !== "database_only"
        || this.#last.model.access.mode !== "read_write") {
      return this.#result(intent, "unavailable", "Journal editing is currently paused.");
    }
    if (intent.instance_id === undefined || intent.client_mutation_id === undefined
        || !isRecord(intent.payload)) {
      return this.#result(intent, "rejected", "That Journal document request is invalid.");
    }
    const localDate = intent.payload.local_date;
    const moduleVersion = intent.payload.module_instance_version;
    if (typeof localDate !== "string" || typeof moduleVersion !== "number"
        || !Number.isSafeInteger(moduleVersion) || moduleVersion < 1) {
      return this.#result(intent, "rejected", "That Journal document request is invalid.");
    }
    const body = {
      clientMutationId: intent.client_mutation_id,
      moduleInstanceVersion: moduleVersion,
    };
    try {
      const headers = await exactHumanAuthorityHeaders(
        {
          action: "journal.document.open",
          subject: `journal-document:${localDate}:${intent.instance_id}`,
          context: body,
        },
        this.#fetch,
      );
      const response = await this.#fetch(
        `${JOURNAL_DOCUMENT_MODULE_ENDPOINT}/${encodeURIComponent(localDate)}`
          + `/${encodeURIComponent(intent.instance_id)}/open`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", ...headers },
          body: JSON.stringify(body),
        },
      );
      const payload = await response.json() as unknown;
      if (!response.ok || !isRecord(payload) || payload.ok !== true) {
        const message = isRecord(payload) && isRecord(payload.error)
          && typeof payload.error.message === "string"
          ? payload.error.message
          : "Co-work could not open that Journal document.";
        return this.#result(
          intent,
          response.status === 409 ? "conflict" : "rejected",
          message,
        );
      }
      const document = documentModuleState(payload.document);
      if (document.state !== "current") {
        return this.#result(intent, "rejected", "Co-work did not return a document target.");
      }
      this.#last = undefined;
      return {
        intent_id: intent.intent_id,
        client_mutation_id: intent.client_mutation_id,
        status: "accepted",
        message: "Journal document opened.",
        value: document,
      };
    } catch (error) {
      return this.#result(
        intent,
        "unavailable",
        error instanceof Error ? error.message : "Co-work is unavailable.",
      );
    }
  }

  async #actOnItem(
    intent: DashboardIntent,
    noteOperation?: "edit" | "tombstone" | "restore",
  ): Promise<IntentResult> {
    const model = this.#last?.model;
    const authorityState = model?.effectiveComposition?.authorityState;
    if (authorityState !== "database_only"
        || model?.access.mode !== "read_write") {
      return this.#result(
        intent,
        "unavailable",
        noteOperation !== undefined && (authorityState === undefined || authorityState === "legacy_compatibility")
          ? "Open this note in Co-work to make changes."
          : "Journal editing is currently paused.",
      );
    }
    if (intent.client_mutation_id === undefined || !isRecord(intent.payload)) {
      return this.#result(intent, "rejected", "That Journal item action is invalid.");
    }
    const payload = intent.payload;
    const itemId = payload.item_id;
    const expectedRevision = noteOperation === undefined
      ? payload.expected_revision
      : payload.expected_version;
    const operation = noteOperation ?? payload.action;
    const allowed = new Set(["edit", "correct", "resolve", "route", "tombstone", "restore"]);
    if (typeof itemId !== "string" || !itemId || typeof expectedRevision !== "number"
        || !Number.isSafeInteger(expectedRevision) || expectedRevision < 1
        || typeof operation !== "string" || !allowed.has(operation)) {
      return this.#result(intent, "rejected", "That Journal item action is invalid.");
    }
    const exactText = noteOperation === "edit" ? payload.markdown : payload.exact_text;
    if ((operation === "edit" || operation === "correct")
        && (typeof exactText !== "string" || exactText.length === 0)) {
      return this.#result(intent, "rejected", "Enter the Journal text to save.");
    }
    const targetDomain = payload.target_domain;
    const targetId = payload.target_id;
    if (operation === "route" && (typeof targetDomain !== "string"
        || typeof targetId !== "string" || targetId.trim().length === 0)) {
      return this.#result(intent, "rejected", "Choose a route destination.");
    }
    const body = {
      clientMutationId: intent.client_mutation_id,
      expectedRevision,
      ...(typeof exactText === "string" ? { exactText } : {}),
      ...(typeof targetDomain === "string" ? { targetDomain } : {}),
      ...(typeof targetId === "string" ? { targetId } : {}),
      ...(typeof payload.target_revision === "string"
        ? { targetRevision: payload.target_revision }
        : {}),
    };
    try {
      const headers = await exactHumanAuthorityHeaders(
        {
          action: `journal.item.${operation}`,
          subject: `journal-item:${itemId}`,
          context: body,
        },
        this.#fetch,
      );
      const response = await this.#fetch(
        `${JOURNAL_ITEMS_ENDPOINT}/${encodeURIComponent(itemId)}/${operation}`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", ...headers },
          body: JSON.stringify(body),
        },
      );
      const result = await response.json() as unknown;
      if (!response.ok || !isRecord(result) || result.ok !== true
          || !isRecord(result.item)) {
        const message = isRecord(result) && isRecord(result.error)
          && typeof result.error.message === "string"
          ? result.error.message
          : "That Journal item action could not be completed.";
        return this.#result(intent, response.status === 409 ? "conflict" : "rejected", message);
      }
      nativeModuleItem(result.item);
      this.#last = undefined;
      return this.#result(intent, "accepted", `Journal item ${operation} saved.`);
    } catch (error) {
      return this.#result(
        intent,
        "unavailable",
        error instanceof Error ? error.message : "That Journal item action is unavailable.",
      );
    }
  }

  async #createPromptInteraction(intent: DashboardIntent): Promise<IntentResult> {
    if (this.#last?.model?.effectiveComposition?.authorityState !== "database_only"
        || this.#last.model.access.mode !== "read_write") {
      return this.#result(intent, "unavailable", "Journal editing is currently paused.");
    }
    if (intent.client_mutation_id === undefined || !isRecord(intent.payload)) {
      return this.#result(intent, "rejected", "That prompt seed is invalid.");
    }
    const payload = intent.payload;
    if (typeof payload.local_date !== "string" || typeof payload.module_instance_id !== "string"
        || payload.module_instance_id !== intent.instance_id
        || typeof payload.module_instance_version !== "number"
        || typeof payload.prompt_id !== "string" || typeof payload.prompt_version !== "number"
        || typeof payload.exact_input !== "string" || payload.exact_input.length === 0) {
      return this.#result(intent, "rejected", "That prompt seed is invalid.");
    }
    const body = {
      clientMutationId: intent.client_mutation_id,
      localDate: payload.local_date,
      moduleInstanceId: payload.module_instance_id,
      moduleInstanceVersion: payload.module_instance_version,
      promptId: payload.prompt_id,
      promptVersion: payload.prompt_version,
      exactInput: payload.exact_input,
      statedAt: new Date().toISOString(),
      resultRetention: "all_versions",
      resultSearchMode: "content",
    };
    return this.#promptMutation(
      intent,
      JOURNAL_PROMPT_INTERACTIONS_ENDPOINT,
      "journal.prompt.create",
      `journal-prompt:${payload.local_date}:${payload.module_instance_id}:${payload.prompt_id}`,
      body,
      "Prompt seed saved separately from generated results.",
    );
  }

  async #generatePromptResult(intent: DashboardIntent): Promise<IntentResult> {
    if (this.#last?.model?.effectiveComposition?.authorityState !== "database_only"
        || this.#last.model.access.mode !== "read_write") {
      return this.#result(intent, "unavailable", "Journal editing is currently paused.");
    }
    if (intent.client_mutation_id === undefined || !isRecord(intent.payload)
        || typeof intent.payload.interaction_id !== "string"
        || typeof intent.payload.expected_revision !== "number") {
      return this.#result(intent, "rejected", "That generation request is invalid.");
    }
    const interactionId = intent.payload.interaction_id;
    const body = {
      clientMutationId: intent.client_mutation_id,
      expectedRevision: intent.payload.expected_revision,
    };
    try {
      const headers = await exactHumanAuthorityHeaders(
        {
          action: "journal.prompt.generate",
          subject: `journal-prompt:${interactionId}`,
          context: body,
        },
        this.#fetch,
      );
      const response = await this.#fetch(
        `${JOURNAL_PROMPT_INTERACTIONS_ENDPOINT}/${encodeURIComponent(interactionId)}/generate`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", ...headers },
          body: JSON.stringify(body),
        },
      );
      const result = await response.json() as unknown;
      if (!response.ok || !isRecord(result) || result.ok !== true) {
        const message = isRecord(result) && isRecord(result.error)
          && typeof result.error.message === "string"
          ? result.error.message
          : "Generation could not start.";
        this.#last = undefined;
        return this.#result(
          intent,
          response.status === 409 ? "conflict" : response.status === 503 ? "unavailable" : "rejected",
          message,
        );
      }
      const generation = promptGeneration(result.generation);
      const message = typeof result.message === "string"
        ? result.message
        : generation.status === "succeeded"
          ? "This generation request already completed."
          : generation.retryable
            ? "A previous generation failed. Choose Generate again to retry."
            : "Generation is in progress.";
      this.#last = undefined;
      if (generation.status === "failed" || generation.status === "expired") {
        this.#updateGenerationPolling(false);
        return this.#result(intent, "unavailable", message);
      }
      if (generation.status === "pending" || generation.status === "leased") {
        this.#updateGenerationPolling(true);
      }
      return this.#result(intent, "accepted", message);
    } catch (error) {
      return this.#result(
        intent,
        "unavailable",
        error instanceof Error ? error.message : "Generation is unavailable.",
      );
    }
  }

  async #decidePromptResult(intent: DashboardIntent): Promise<IntentResult> {
    if (this.#last?.model?.effectiveComposition?.authorityState !== "database_only"
        || this.#last.model.access.mode !== "read_write") {
      return this.#result(intent, "unavailable", "Journal editing is currently paused.");
    }
    if (intent.client_mutation_id === undefined || !isRecord(intent.payload)
        || typeof intent.payload.interaction_id !== "string"
        || typeof intent.payload.variant_id !== "string"
        || typeof intent.payload.expected_revision !== "number"
        || (intent.payload.decision !== "accept" && intent.payload.decision !== "archive"
          && intent.payload.decision !== "reject")) {
      return this.#result(intent, "rejected", "That prompt result decision is invalid.");
    }
    const interactionId = intent.payload.interaction_id;
    const variantId = intent.payload.variant_id;
    const body = {
      clientMutationId: intent.client_mutation_id,
      expectedRevision: intent.payload.expected_revision,
      decision: intent.payload.decision,
    };
    return this.#promptMutation(
      intent,
      `${JOURNAL_PROMPT_INTERACTIONS_ENDPOINT}/${encodeURIComponent(interactionId)}/variants/${encodeURIComponent(variantId)}/decide`,
      "journal.prompt.decide",
      `journal-prompt:${interactionId}:${variantId}`,
      body,
      "Prompt result decision saved.",
    );
  }

  async #promptMutation(
    intent: DashboardIntent,
    endpoint: string,
    action: string,
    subject: string,
    body: Readonly<Record<string, unknown>>,
    successMessage: string,
  ): Promise<IntentResult> {
    try {
      const headers = await exactHumanAuthorityHeaders(
        { action, subject, context: body },
        this.#fetch,
      );
      const response = await this.#fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...headers },
        body: JSON.stringify(body),
      });
      const result = await response.json() as unknown;
      if (!response.ok || !isRecord(result) || result.ok !== true) {
        const message = isRecord(result) && isRecord(result.error)
          && typeof result.error.message === "string"
          ? result.error.message
          : "That prompt action could not be completed.";
        this.#last = undefined;
        return this.#result(
          intent,
          response.status === 409 ? "conflict" : response.status === 503 ? "unavailable" : "rejected",
          message,
        );
      }
      this.#last = undefined;
      return this.#result(intent, "accepted", successMessage);
    } catch (error) {
      return this.#result(
        intent,
        "unavailable",
        error instanceof Error ? error.message : "That prompt action is unavailable.",
      );
    }
  }

  async #putFieldValue(intent: DashboardIntent): Promise<IntentResult> {
    if (intent.client_mutation_id === undefined || !isRecord(intent.payload)) {
      return this.#result(intent, "rejected", "That Journal field edit is invalid.");
    }
    const payload = intent.payload;
    const localDate = payload.local_date;
    const moduleId = payload.module_instance_id;
    const moduleVersion = payload.module_instance_version;
    const slotId = payload.composition_slot_id;
    const fieldId = payload.field_id;
    const fieldVersion = payload.field_definition_version;
    const expectedRevision = payload.expected_revision;
    const exactInput = payload.exact_input;
    const valueId = payload.value_id;
    const disposition = payload.disposition;
    if (
      typeof localDate !== "string"
      || typeof moduleId !== "string"
      || moduleId !== intent.instance_id
      || typeof moduleVersion !== "number"
      || !Number.isInteger(moduleVersion)
      || moduleVersion < 1
      || typeof slotId !== "string"
      || typeof fieldId !== "string"
      || typeof fieldVersion !== "number"
      || !Number.isInteger(fieldVersion)
      || fieldVersion < 1
      || typeof expectedRevision !== "number"
      || !Number.isInteger(expectedRevision)
      || expectedRevision < 0
      || typeof exactInput !== "string"
      || (valueId !== undefined && typeof valueId !== "string")
      || (disposition !== undefined
        && disposition !== "missing"
        && disposition !== "skipped"
        && disposition !== "declined")
    ) {
      return this.#result(intent, "rejected", "That Journal field edit is invalid.");
    }
    const composition = this.#last?.model?.effectiveComposition;
    const module = composition?.modules.find(
      (candidate) => candidate.moduleInstanceId === moduleId
        && candidate.moduleInstanceVersion === moduleVersion
        && candidate.semanticMembership === "included",
    );
    const field = module?.fields.find(
      (candidate) => candidate.compositionSlotId === slotId
        && candidate.fieldId === fieldId
        && candidate.fieldDefinitionVersion === fieldVersion,
    );
    if (
      composition?.authorityState !== "database_only"
      || this.#last?.model?.access.mode !== "read_write"
      || module === undefined
      || field === undefined
    ) {
      return this.#result(
        intent,
        "unavailable",
        composition?.authorityState === "recovery_fenced"
          ? "Journal recovery is still reconciling. Editing is paused."
          : "That Journal field is not currently editable. Refresh and try again.",
      );
    }
    const body = {
      clientMutationId: intent.client_mutation_id,
      localDate,
      moduleInstanceId: moduleId,
      moduleInstanceVersion: moduleVersion,
      compositionSlotId: slotId,
      fieldId,
      fieldDefinitionVersion: fieldVersion,
      ...(valueId === undefined ? {} : { valueId }),
      expectedRevision,
      value: payload.value,
      ...(disposition === undefined ? {} : { disposition }),
      exactInput,
      ...(typeof payload.stated_at === "string" ? { statedAt: payload.stated_at } : {}),
    };
    try {
      const headers = await exactHumanAuthorityHeaders(
        {
          action: "journal.field_value.put",
          subject: `journal-field:${localDate}:${moduleId}:${fieldId}`,
          context: body,
        },
        this.#fetch,
      );
      const response = await this.#fetch(JOURNAL_FIELD_VALUES_ENDPOINT, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...headers },
        body: JSON.stringify(body),
      });
      const result = await response.json() as unknown;
      if (!response.ok || !isRecord(result) || result.ok !== true
          || !isRecord(result.fieldValue)) {
        const message = isRecord(result) && isRecord(result.error)
          && typeof result.error.message === "string"
          ? result.error.message
          : "That Journal field could not be saved. Your input is still in the editor.";
        return this.#result(
          intent,
          response.status === 409 ? "conflict" : "rejected",
          message,
        );
      }
      fieldValue(result.fieldValue);
      this.#last = undefined;
      return this.#result(intent, "accepted", "Journal field saved.");
    } catch (error) {
      return this.#result(
        intent,
        "unavailable",
        error instanceof Error
          ? error.message
          : "That Journal field could not be saved. Your input is still in the editor.",
      );
    }
  }

  async #retryCapture(intent: DashboardIntent): Promise<IntentResult> {
    if (!isRecord(intent.payload) || typeof intent.payload.capture_id !== "string"
        || !/^[0-9a-f]{32}$/.test(intent.payload.capture_id)
        || typeof intent.payload.expected_revision !== "number" || !Number.isInteger(intent.payload.expected_revision)) {
      return this.#result(intent, "rejected", "The capture retry is invalid.");
    }
    const captureId = intent.payload.capture_id;
    const capture = this.#last?.model?.widgetInputs[JOURNAL_WIDGET_INSTANCE_IDS.capture].recentSubmissions.find((item) => item.captureId === captureId);
    if (capture === undefined) return this.#result(intent, "rejected", "Refresh Journal before retrying this capture.");
    const disclosureSha256 = intent.payload.smart_disclosure_sha256;
    if ((capture.mode === "smart" && disclosureSha256 === undefined)
        || (disclosureSha256 !== undefined && (typeof disclosureSha256 !== "string" || !/^[0-9a-f]{64}$/.test(disclosureSha256)))) {
      return this.#result(intent, "rejected", "Review the current Smart disclosure before retrying.");
    }
    const expectedRevision = intent.payload.expected_revision;
    try {
      const identity = await initializeLocalIdentity({ fetchImpl: this.#fetch });
      if (!identity.authenticated) return this.#result(intent, "unavailable", "Reconnect to retry this capture.");
      const contextSha256 = await sha256Hex(`wb.journal-capture-retry/v1:${captureId}:${expectedRevision}${disclosureSha256 ? `:${disclosureSha256}` : ""}`);
      const gesture = await issueHumanGesture({ action: "journal.capture.retry", subject: `journal-capture:${captureId}`, contextSha256 }, this.#fetch);
      const response = await this.#fetch(`${JOURNAL_CAPTURE_ENDPOINT}/${captureId}/retry`, {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", ...localIdentityHeaders(gesture.token) },
        body: JSON.stringify(disclosureSha256 ? { smart_disclosure_sha256: disclosureSha256 } : {}),
      });
      const payload = await response.json() as unknown;
      if (!response.ok || !isRecord(payload) || payload.ok !== true) {
        return this.#result(intent, response.status === 409 ? "conflict" : "rejected", "The follow-up could not finish. Your saved capture is safe.");
      }
      captureSubmission(payload.capture);
      return this.#result(intent, "accepted", "Capture follow-up retried.");
    } catch {
      return this.#result(intent, "unavailable", "The follow-up is unavailable. Your saved capture is safe.");
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
          "Editing is paused in this browser. Open Journal from the Work Buddy tray to reconnect.",
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
