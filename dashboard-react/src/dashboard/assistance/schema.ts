import canonicalManifest from "../../../../work_buddy/dashboard/assistance/form_schemas.json";
import type { AssistableDraftDeclaration, JsonObject, JsonValue } from "../contributions/contracts";
import { canonicalHumanAuthorityJson } from "../../security/humanAuthority";
import { sha256Hex } from "../../security/localIdentity";
import type { AssistedField, AssistedFormSchema, DraftPatchOperation } from "./contracts";

export const assistedForms = canonicalManifest.forms as unknown as Readonly<Record<string, AssistedFormSchema>>;
const forbidden = new Set(["__proto__", "prototype", "constructor"]);
export const pathKey = (path: readonly string[]) => JSON.stringify(path);
export const equalJson = (left: unknown, right: unknown): boolean =>
  left === undefined || right === undefined ? left === right : canonicalHumanAuthorityJson(left) === canonicalHumanAuthorityJson(right);
export const snapshotHash = (value: JsonObject): Promise<string> => sha256Hex(canonicalHumanAuthorityJson(value));
export const byteLength = (value: unknown) => new TextEncoder().encode(canonicalHumanAuthorityJson(value)).byteLength;
export const isRecord = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);

export function assistedDraftDeclaration(draftName: string): AssistableDraftDeclaration {
  const form = assistedForms[draftName];
  if (form === undefined) throw new Error("Unknown assisted form schema");
  return { draftName, schema: form.schema, submitPolicy: "user_only" };
}

export function fieldFor(form: AssistedFormSchema, path: unknown): AssistedField {
  if (!Array.isArray(path) || path.length === 0 || path.some((part: unknown) => typeof part !== "string" || !part || forbidden.has(part) || part === "*")) throw new Error("Invalid assisted field path");
  const field = form.fields.find((candidate) => equalJson(candidate.path, path));
  if (!field || field.sensitivity === "secret" || field.disclosure !== "explicit_start") throw new Error("Field is not assistable");
  return field;
}

export function readField(value: unknown, path: readonly string[]): JsonValue | undefined {
  let current: unknown = value;
  for (const part of path) {
    if (!isRecord(current) || !Object.prototype.hasOwnProperty.call(current, part)) return undefined;
    current = current[part];
  }
  return current as JsonValue | undefined;
}

export function writeField(value: JsonObject, path: readonly string[], next: JsonValue): JsonObject {
  const clone = JSON.parse(JSON.stringify(value)) as Record<string, JsonValue>;
  let current = clone;
  for (const part of path.slice(0, -1)) {
    if (forbidden.has(part)) throw new Error("Invalid assisted field path");
    if (!isRecord(current[part])) current[part] = {};
    current = current[part] as Record<string, JsonValue>;
  }
  const final = path[path.length - 1];
  if (!final || forbidden.has(final)) throw new Error("Invalid assisted field path");
  current[final] = next;
  return clone;
}

export function restoreField(value: JsonObject, path: readonly string[], next: JsonValue | undefined): JsonObject {
  if (next !== undefined) return writeField(value, path, next);
  const clone = JSON.parse(JSON.stringify(value)) as Record<string, JsonValue>;
  let current = clone;
  for (const part of path.slice(0, -1)) {
    if (forbidden.has(part) || !isRecord(current[part])) return clone;
    current = current[part] as Record<string, JsonValue>;
  }
  const final = path[path.length - 1];
  if (!final || forbidden.has(final)) throw new Error("Invalid assisted field path");
  delete current[final];
  return clone;
}

export function validateFieldValue(field: AssistedField, value: unknown): asserts value is JsonValue {
  if (typeof value !== field.type || (typeof value === "number" && !Number.isFinite(value))) throw new Error("Invalid assisted field type");
  if (field.enum && !field.enum.some((candidate) => equalJson(candidate, value))) throw new Error("Invalid assisted field value");
  if (typeof value === "string" && (Array.from(value).length > (field.maxLength ?? 8192) || (field.pattern && !new RegExp(field.pattern).test(value)))) throw new Error("Invalid assisted field value");
  if (typeof value === "number" && (value < (field.minimum ?? -Infinity) || value > (field.maximum ?? Infinity))) throw new Error("Invalid assisted field value");
  if (field.contentPolicy === "non_secret_json" && typeof value === "string" && value.trim()) {
    let parameters: unknown;
    try { parameters = JSON.parse(value); } catch { throw new Error("Parameters must be valid JSON before assistance can inspect them."); }
    const inspect = (item: unknown): void => {
      if (Array.isArray(item)) item.forEach(inspect);
      else if (isRecord(item)) for (const [key, child] of Object.entries(item)) {
        if (forbidden.has(key) || /password|passwd|secret|token|api[_-]?key|credential|authorization|private[_-]?key/i.test(key)) throw new Error("Secret parameters cannot be disclosed to form assistance. Remove credentials from this form before starting.");
        inspect(child);
      }
    };
    inspect(parameters);
  }
}

/** Pick, never serialize the whole draft. Host-only metadata stays in the host. */
export function discloseSnapshot(form: AssistedFormSchema, draft: unknown): JsonObject {
  let result: JsonObject = {};
  for (const field of form.fields) {
    if (field.sensitivity === "secret" || field.disclosure !== "explicit_start") continue;
    const value = readField(draft, field.path);
    if (value === undefined) continue;
    validateFieldValue(field, value);
    result = writeField(result, field.path, value);
  }
  if (byteLength(result) > form.maxSnapshotBytes) throw new Error("The disclosed draft is too large");
  return result;
}

export function validateSnapshot(form: AssistedFormSchema, value: unknown): asserts value is JsonObject {
  if (!isRecord(value) || byteLength(value) > form.maxSnapshotBytes) throw new Error("Invalid draft snapshot");
  const walk = (node: Record<string, unknown>, prefix: readonly string[]) => {
    for (const [key, item] of Object.entries(node)) {
      if (forbidden.has(key)) throw new Error("Invalid assisted field path");
      const path = [...prefix, key];
      if (isRecord(item)) {
        if (!form.fields.some((field) => equalJson(field.path.slice(0, path.length), path))) throw new Error("Field is not assistable");
        walk(item, path);
      } else validateFieldValue(fieldFor(form, path), item);
    }
  };
  walk(value, []);
}

export function validateOperations(form: AssistedFormSchema, value: unknown): asserts value is readonly DraftPatchOperation[] {
  if (!Array.isArray(value) || value.length > form.maxOperations || byteLength(value) > form.maxPatchBytes) throw new Error("Invalid patch operations");
  const seen = new Set<string>();
  for (const operation of value) {
    if (!isRecord(operation) || (operation.op !== "set" && operation.op !== "remove") || !form.allowedOperations.includes(operation.op)) throw new Error("Invalid patch operation");
    const keys = Object.keys(operation).sort();
    if (!equalJson(keys, operation.op === "set" ? ["op", "path", "value"] : ["op", "path"])) throw new Error("Invalid patch operation");
    const field = fieldFor(form, operation.path);
    const key = pathKey(field.path);
    if (seen.has(key)) throw new Error("Duplicate patch field");
    seen.add(key);
    if (operation.op === "remove") {
      if (field.required) throw new Error("Required fields cannot be removed");
    } else validateFieldValue(field, operation.value);
  }
}
