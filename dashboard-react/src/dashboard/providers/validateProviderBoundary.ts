import type {
  DashboardIntent,
  IntentResult,
  ReconcileResult,
  SnapshotRevision,
  SnapshotStatus,
  ViewId,
  ViewSnapshot,
  WidgetInstanceId,
  WidgetSnapshot,
  WidgetTypeId,
} from "../contributions/contracts";
import { isOpaqueWidgetInstanceId } from "../contributions/contracts";
import { validateJsonValue } from "../contributions/validate";

const SNAPSHOT_STATUSES = new Set<SnapshotStatus>([
  "ready",
  "stale",
  "offline",
  "unavailable",
  "permission-denied",
  "read-only",
  "error",
]);
const QUALITY_KINDS = new Set(["complete", "partial", "demo"]);
const INTENT_RESULTS = new Set(["accepted", "rejected", "conflict", "unavailable"]);
const NAMESPACED_ID = /^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$/;
const ROLE_ID = /^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+@[1-9]\d*$/;
const COMPOSITION_SLOT_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
const SLOT_PRESENCES = new Set(["required", "default_on", "default_off"]);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const revisionIsValid = (value: unknown): value is SnapshotRevision =>
  (typeof value === "string" && value.trim().length > 0) ||
  (typeof value === "number" && Number.isFinite(value));

export class ProviderContractError extends Error {
  constructor(message: string) {
    super(`Dashboard View API contract violation: ${message}`);
    this.name = "ProviderContractError";
  }
}

const requireJson = (value: unknown, path: string): void => {
  const issues = validateJsonValue(value, path);
  if (issues.length > 0) {
    const first = issues[0]!;
    throw new ProviderContractError(`${first.path} ${first.message}`);
  }
};

const requireRevision = (value: unknown, path: string): void => {
  if (value !== undefined && !revisionIsValid(value)) {
    throw new ProviderContractError(`${path} must be a non-empty string or finite number`);
  }
};

const requireCompositionOrder = (
  value: unknown,
  path: string,
  slotIds: ReadonlySet<string>,
  requiredSlotIds: ReadonlySet<string>,
): void => {
  if (!Array.isArray(value)) {
    throw new ProviderContractError(`${path} must be an array`);
  }
  const seen = new Set<string>();
  value.forEach((slotId, index) => {
    if (typeof slotId !== "string" || !COMPOSITION_SLOT_ID.test(slotId)) {
      throw new ProviderContractError(`${path}[${index}] must be a valid slot ID`);
    }
    if (seen.has(slotId)) {
      throw new ProviderContractError(`${path} must not contain duplicate slot ${slotId}`);
    }
    if (!slotIds.has(slotId)) {
      throw new ProviderContractError(`${path}[${index}] references unknown slot ${slotId}`);
    }
    seen.add(slotId);
  });
  for (const slotId of requiredSlotIds) {
    if (!seen.has(slotId)) {
      throw new ProviderContractError(`${path} must include slot ${slotId}`);
    }
  }
};

const requireEffectiveComposition = (
  value: unknown,
  widgetInputs: Readonly<Record<string, unknown>>,
): void => {
  const path = "snapshot.effectiveComposition";
  if (!isRecord(value)) {
    throw new ProviderContractError(`${path} must be an object`);
  }
  requireJson(value, path);
  if (typeof value.compositionId !== "string" || value.compositionId.trim().length === 0) {
    throw new ProviderContractError(`${path}.compositionId must be a non-empty string`);
  }
  if (!revisionIsValid(value.revision)) {
    throw new ProviderContractError(
      `${path}.revision must be a non-empty string or finite number`,
    );
  }
  if (!Array.isArray(value.defaultSlots)) {
    throw new ProviderContractError(`${path}.defaultSlots must be an array`);
  }

  const slotIds = new Set<string>();
  const instanceIds = new Set<string>();
  const mobileRequiredSlotIds = new Set<string>();
  value.defaultSlots.forEach((slot, index) => {
    const slotPath = `${path}.defaultSlots[${index}]`;
    if (!isRecord(slot)) {
      throw new ProviderContractError(`${slotPath} must be an object`);
    }
    if (typeof slot.slotId !== "string" || !COMPOSITION_SLOT_ID.test(slot.slotId)) {
      throw new ProviderContractError(`${slotPath}.slotId must be a valid slot ID`);
    }
    if (slotIds.has(slot.slotId)) {
      throw new ProviderContractError(`${path}.defaultSlots must not duplicate slot ${slot.slotId}`);
    }
    slotIds.add(slot.slotId);

    if (!isOpaqueWidgetInstanceId(slot.defaultInstanceId)) {
      throw new ProviderContractError(
        `${slotPath}.defaultInstanceId must be an opaque instance ID`,
      );
    }
    if (instanceIds.has(slot.defaultInstanceId)) {
      throw new ProviderContractError(
        `${path}.defaultSlots must not duplicate instance ${slot.defaultInstanceId}`,
      );
    }
    instanceIds.add(slot.defaultInstanceId);
    if (!Object.prototype.hasOwnProperty.call(widgetInputs, slot.defaultInstanceId)) {
      throw new ProviderContractError(
        `snapshot.widgetInputs must contain composed instance ${slot.defaultInstanceId}`,
      );
    }

    if (typeof slot.requiredRole !== "string" || !ROLE_ID.test(slot.requiredRole)) {
      throw new ProviderContractError(`${slotPath}.requiredRole must be a versioned role ID`);
    }
    if (
      typeof slot.defaultWidgetTypeId !== "string" ||
      !NAMESPACED_ID.test(slot.defaultWidgetTypeId)
    ) {
      throw new ProviderContractError(
        `${slotPath}.defaultWidgetTypeId must be a namespaced widget type ID`,
      );
    }
    if (typeof slot.presence !== "string" || !SLOT_PRESENCES.has(slot.presence)) {
      throw new ProviderContractError(
        `${slotPath}.presence must be required, default_on, or default_off`,
      );
    }
    if (slot.presence !== "default_off") mobileRequiredSlotIds.add(slot.slotId);

    if (
      !isRecord(slot.help) ||
      typeof slot.help.summary !== "string" ||
      slot.help.summary.trim().length === 0 ||
      typeof slot.help.details !== "string" ||
      slot.help.details.trim().length === 0
    ) {
      throw new ProviderContractError(
        `${slotPath}.help must contain non-empty summary and details strings`,
      );
    }
    if (!isRecord(slot.defaultLayout)) {
      throw new ProviderContractError(`${slotPath}.defaultLayout must be an object`);
    }
    const { x, y, w, h } = slot.defaultLayout;
    if (
      !Number.isInteger(x) ||
      !Number.isInteger(y) ||
      !Number.isInteger(w) ||
      !Number.isInteger(h) ||
      (x as number) < 0 ||
      (y as number) < 0 ||
      (w as number) < 1 ||
      (h as number) < 1
    ) {
      throw new ProviderContractError(
        `${slotPath}.defaultLayout must use non-negative integer coordinates and positive integer size`,
      );
    }
    requireJson(slot.defaultSettings, `${slotPath}.defaultSettings`);
    if (slot.defaultBindings !== undefined) {
      if (!isRecord(slot.defaultBindings)) {
        throw new ProviderContractError(`${slotPath}.defaultBindings must be an object`);
      }
      requireJson(slot.defaultBindings, `${slotPath}.defaultBindings`);
    }
  });

  requireCompositionOrder(value.readingOrder, `${path}.readingOrder`, slotIds, slotIds);
  requireCompositionOrder(
    value.mobileOrder,
    `${path}.mobileOrder`,
    slotIds,
    mobileRequiredSlotIds,
  );
};

export function assertViewSnapshot(
  value: unknown,
  expectedViewId: ViewId,
): asserts value is ViewSnapshot {
  if (!isRecord(value)) throw new ProviderContractError("snapshot must be an object");
  if (value.viewId !== expectedViewId) {
    throw new ProviderContractError(
      `snapshot.viewId ${String(value.viewId)} does not match ${expectedViewId}`,
    );
  }
  requireRevision(value.revision, "snapshot.revision");
  if (typeof value.observedAt !== "string" || !Number.isFinite(Date.parse(value.observedAt))) {
    throw new ProviderContractError("snapshot.observedAt must be an ISO-compatible instant");
  }
  if (!SNAPSHOT_STATUSES.has(value.status as SnapshotStatus)) {
    throw new ProviderContractError(`snapshot.status ${String(value.status)} is unsupported`);
  }
  if (!isRecord(value.quality) || !QUALITY_KINDS.has(value.quality.kind as string)) {
    throw new ProviderContractError("snapshot.quality.kind must be complete, partial, or demo");
  }
  if (value.quality.message !== undefined && typeof value.quality.message !== "string") {
    throw new ProviderContractError("snapshot.quality.message must be a string");
  }
  if (!isRecord(value.bindings)) {
    throw new ProviderContractError("snapshot.bindings must be an object");
  }
  if (!isRecord(value.widgetInputs)) {
    throw new ProviderContractError("snapshot.widgetInputs must be an object");
  }
  for (const instanceId of Object.keys(value.widgetInputs)) {
    if (!isOpaqueWidgetInstanceId(instanceId)) {
      throw new ProviderContractError(
        `snapshot.widgetInputs key ${JSON.stringify(instanceId)} is not an opaque instance ID`,
      );
    }
  }
  if (value.effectiveComposition !== undefined) {
    requireEffectiveComposition(value.effectiveComposition, value.widgetInputs);
  }
  requireJson(value.model, "snapshot.model");
  requireJson(value.bindings, "snapshot.bindings");
  requireJson(value.widgetInputs, "snapshot.widgetInputs");
}

export function assertWidgetSnapshot(
  value: unknown,
  expectedWidgetTypeId: WidgetTypeId,
  expectedInstanceId: WidgetInstanceId,
  expectedRevision?: SnapshotRevision,
): asserts value is WidgetSnapshot {
  if (!isRecord(value)) throw new ProviderContractError("widget snapshot must be an object");
  if (
    !isOpaqueWidgetInstanceId(expectedInstanceId) ||
    !isOpaqueWidgetInstanceId(value.instanceId)
  ) {
    throw new ProviderContractError("widget snapshot.instanceId must be an opaque instance ID");
  }
  if (value.widgetTypeId !== expectedWidgetTypeId) {
    throw new ProviderContractError(
      `widget snapshot.widgetTypeId ${String(value.widgetTypeId)} does not match ${expectedWidgetTypeId}`,
    );
  }
  if (value.instanceId !== expectedInstanceId) {
    throw new ProviderContractError(
      `widget snapshot.instanceId ${String(value.instanceId)} does not match ${expectedInstanceId}`,
    );
  }
  requireRevision(value.revision, "widget snapshot.revision");
  if (expectedRevision !== undefined && !Object.is(value.revision, expectedRevision)) {
    throw new ProviderContractError(
      `widget snapshot.revision ${String(value.revision)} does not match view revision ${String(expectedRevision)}`,
    );
  }
  if (typeof value.observedAt !== "string" || !Number.isFinite(Date.parse(value.observedAt))) {
    throw new ProviderContractError("widget snapshot.observedAt must be an ISO-compatible instant");
  }
  if (!SNAPSHOT_STATUSES.has(value.status as SnapshotStatus)) {
    throw new ProviderContractError(
      `widget snapshot.status ${String(value.status)} is unsupported`,
    );
  }
  if (!isRecord(value.quality) || !QUALITY_KINDS.has(value.quality.kind as string)) {
    throw new ProviderContractError(
      "widget snapshot.quality.kind must be complete, partial, or demo",
    );
  }
  if (value.quality.message !== undefined && typeof value.quality.message !== "string") {
    throw new ProviderContractError("widget snapshot.quality.message must be a string");
  }
  requireJson(value.input, "widget snapshot.input");
}

export function assertDashboardIntent(
  value: unknown,
  expectedViewId: ViewId,
): asserts value is DashboardIntent {
  if (!isRecord(value)) throw new ProviderContractError("intent must be an object");
  if (typeof value.intent_type !== "string" || !NAMESPACED_ID.test(value.intent_type)) {
    throw new ProviderContractError("intent.intent_type must be a namespaced ID");
  }
  if (!Number.isInteger(value.schema_version) || (value.schema_version as number) < 1) {
    throw new ProviderContractError("intent.schema_version must be a positive integer");
  }
  if (typeof value.intent_id !== "string" || value.intent_id.trim().length === 0) {
    throw new ProviderContractError("intent.intent_id must be a non-empty string");
  }
  if (value.view_id !== expectedViewId) {
    throw new ProviderContractError(
      `intent.view_id ${String(value.view_id)} does not match ${expectedViewId}`,
    );
  }
  if (
    value.instance_id !== undefined &&
    !isOpaqueWidgetInstanceId(value.instance_id)
  ) {
    throw new ProviderContractError("intent.instance_id must be an opaque instance ID");
  }
  if (
    value.client_mutation_id !== undefined &&
    (typeof value.client_mutation_id !== "string" || value.client_mutation_id.length === 0)
  ) {
    throw new ProviderContractError("intent.client_mutation_id must be a non-empty string");
  }
  requireJson(value.payload, "intent.payload");
}

export function assertIntentResult(
  value: unknown,
  intent: DashboardIntent,
): asserts value is IntentResult {
  if (!isRecord(value)) throw new ProviderContractError("intent result must be an object");
  if (value.intent_id !== intent.intent_id) {
    throw new ProviderContractError("intent result must echo intent_id");
  }
  if (!INTENT_RESULTS.has(value.status as string)) {
    throw new ProviderContractError(`intent result status ${String(value.status)} is unsupported`);
  }
  if (
    value.client_mutation_id !== undefined &&
    value.client_mutation_id !== intent.client_mutation_id
  ) {
    throw new ProviderContractError("intent result client_mutation_id does not match the request");
  }
  requireRevision(value.revision, "intent result.revision");
  if (value.message !== undefined && typeof value.message !== "string") {
    throw new ProviderContractError("intent result.message must be a string");
  }
  if (value.value !== undefined) requireJson(value.value, "intent result.value");
  if (value.fieldErrors !== undefined) {
    if (
      !isRecord(value.fieldErrors) ||
      Object.values(value.fieldErrors).some((entry) => typeof entry !== "string")
    ) {
      throw new ProviderContractError("intent result.fieldErrors must map fields to strings");
    }
  }
}

export function assertReconcileResult(
  value: unknown,
  expectedViewId: ViewId,
): asserts value is ReconcileResult {
  if (!isRecord(value) || typeof value.changed !== "boolean") {
    throw new ProviderContractError("reconcile result.changed must be a boolean");
  }
  requireRevision(value.revision, "reconcile result.revision");
  if (value.snapshot !== undefined) {
    assertViewSnapshot(value.snapshot, expectedViewId);
    if (
      value.revision !== undefined &&
      value.snapshot.revision !== undefined &&
      !Object.is(value.revision, value.snapshot.revision)
    ) {
      throw new ProviderContractError(
        "reconcile result revision must match its embedded snapshot revision",
      );
    }
  }
}
