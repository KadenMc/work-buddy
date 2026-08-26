import type { JsonObject, JsonValue } from "../contributions/contracts";
import type { WidgetDraftIdentity } from "../drafts/contracts";
import type { AssistedDraftPatch, AssistedFormSchema, DraftPatchReceipt } from "./contracts";
import { byteLength, equalJson, fieldFor, isRecord, pathKey, readField, restoreField, snapshotHash, validateOperations, validateSnapshot, writeField } from "./schema";

export interface FieldChange {
  readonly path: readonly string[];
  readonly before: JsonValue | undefined;
  readonly after: JsonValue;
}

export interface PatchPlan {
  readonly value: JsonObject;
  readonly changes: readonly FieldChange[];
  readonly receipt: DraftPatchReceipt;
}

export async function validatePatch(value: unknown, binding: {
  readonly identity: WidgetDraftIdentity;
  readonly assistantSessionId: string;
  readonly conversationId: string;
  readonly form: AssistedFormSchema;
}): Promise<AssistedDraftPatch> {
  const { form } = binding;
  if (!isRecord(value) || byteLength(value) > form.maxSnapshotBytes + form.maxPatchBytes + 4096) throw new Error("Invalid assisted patch");
  if (!equalJson(Object.keys(value).sort(), ["protocol", "assistantSessionId", "conversationId", "identity", "schema", "baseDraftRevision", "baseSnapshotHash", "baseSnapshot", "patchId", "operations"].sort())) throw new Error("Invalid assisted patch envelope");
  if (value.protocol !== "wb.assisted-draft.patch/v1" || value.assistantSessionId !== binding.assistantSessionId || value.conversationId !== binding.conversationId || !equalJson(value.identity, binding.identity) || !equalJson(value.schema, form.schema)) throw new Error("This patch belongs to a different draft binding");
  if (typeof value.baseDraftRevision !== "number" || !Number.isSafeInteger(value.baseDraftRevision) || value.baseDraftRevision < 0 || typeof value.patchId !== "string" || value.patchId.length === 0 || value.patchId.length > 256) throw new Error("Invalid assisted patch identity or revision");
  validateSnapshot(form, value.baseSnapshot);
  validateOperations(form, value.operations);
  if (value.baseSnapshotHash !== await snapshotHash(value.baseSnapshot)) throw new Error("Assisted patch snapshot hash mismatch");
  return value as unknown as AssistedDraftPatch;
}

/** Call only after envelope validation, with a fresh synchronous host snapshot. */
export function planPatch(patch: AssistedDraftPatch, form: AssistedFormSchema, current: { readonly value: JsonObject; readonly revision: number }, focused: ReadonlySet<string>, reviewedPaths?: ReadonlySet<string>): PatchPlan {
  if (patch.baseDraftRevision > current.revision) throw new Error("The patch refers to a future draft revision");
  let value = current.value;
  const changes: FieldChange[] = [];
  const pending: DraftPatchReceipt["pendingFields"][number][] = [];
  for (const operation of patch.operations) {
    const key = pathKey(operation.path);
    if (reviewedPaths && !reviewedPaths.has(key)) continue;
    const before = readField(current.value, operation.path);
    const base = readField(patch.baseSnapshot, operation.path);
    const field = fieldFor(form, operation.path);
    const after = operation.op === "set" ? operation.value : field.default;
    const reason = focused.has(key) ? "focused"
      : reviewedPaths ? null
      : !equalJson(before, base) ? "user_changed"
      : form.patchBehavior === "suggest" ? "suggest_only" : null;
    if (reason) {
      pending.push({ path: operation.path, reason });
      continue;
    }
    value = writeField(value, operation.path, after);
    changes.push({ path: operation.path, before, after });
  }
  const status = pending.length ? (changes.length ? "partial" : "pending") : "applied";
  return {
    value, changes,
    receipt: {
      patchId: patch.patchId, status,
      appliedFields: changes.map((change) => change.path), pendingFields: pending,
      resultingRevision: current.revision + (changes.length ? 1 : 0),
      message: `${changes.length} field${changes.length === 1 ? "" : "s"} filled by assistant${pending.length ? `; ${pending.length} suggestion${pending.length === 1 ? "" : "s"} need review` : ""}.`,
    },
  };
}

/** Conditional inverse: never erase a human edit made after the assistant. */
export function planUndo(patchId: string, changes: readonly FieldChange[], current: { readonly value: JsonObject; readonly revision: number }, focused: ReadonlySet<string>): PatchPlan {
  let value = current.value;
  const inverse: FieldChange[] = [];
  const pending: DraftPatchReceipt["pendingFields"][number][] = [];
  for (const change of changes) {
    if (focused.has(pathKey(change.path)) || !equalJson(readField(current.value, change.path), change.after)) {
      pending.push({ path: change.path, reason: focused.has(pathKey(change.path)) ? "focused" : "user_changed" });
      continue;
    }
    value = restoreField(value, change.path, change.before);
    inverse.push(change);
  }
  return {
    value, changes: inverse,
    receipt: {
      patchId, status: "undone", appliedFields: inverse.map((change) => change.path), pendingFields: pending,
      resultingRevision: current.revision + (inverse.length ? 1 : 0),
      message: `Undid ${inverse.length} assistant field change${inverse.length === 1 ? "" : "s"}${pending.length ? `; kept ${pending.length} field${pending.length === 1 ? "" : "s"} you changed or are editing` : ""}.`,
    },
  };
}
