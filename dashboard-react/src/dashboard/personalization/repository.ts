import type { ViewId } from "../contributions/contracts";
import { validateDashboardLayout } from "../layout/operations";
import type { PersonalWidgetInstance, ViewPersonalizationPatch } from "./contracts";

export interface PersonalizationRepository {
  load(viewId: ViewId): Promise<ViewPersonalizationPatch | null>;
  save(patch: ViewPersonalizationPatch): Promise<void>;
  reset(viewId: ViewId): Promise<void>;
}

export class PersonalizationRepositoryError extends Error {
  readonly key: string;
  readonly rawValue?: string;

  constructor(message: string, key: string, rawValue?: string) {
    super(message);
    this.name = "PersonalizationRepositoryError";
    this.key = key;
    this.rawValue = rawValue;
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const validateLayoutRecord = (
  layoutRecord: Record<string, unknown>,
  instanceId: string,
  path: string,
  issues: string[],
): void => {
  const forbidden = ["i", "static", "moved", "isDraggable", "isResizable"];
  if (forbidden.some((field) => field in layoutRecord)) {
    issues.push(`${path} contains RGL-only fields`);
  }
  const layout = {
    ...layoutRecord,
    instanceId,
  } as PersonalWidgetInstance["layout"];
  issues.push(...validateDashboardLayout([layout]).map((issue) => `${path}.${issue}`));
};

const validatePersonalInstance = (
  value: unknown,
  path: string,
  issues: string[],
): void => {
  if (!isRecord(value) || typeof value.instanceId !== "string" || !isRecord(value.layout)) {
    issues.push(`${path} must be a personal widget instance with a layout`);
    return;
  }
  const layoutRecord = value.layout;
  validateLayoutRecord(layoutRecord, value.instanceId, `${path}.layout`, issues);
};

export function parsePersonalizationPatch(raw: string): ViewPersonalizationPatch {
  let value: unknown;
  try {
    value = JSON.parse(raw) as unknown;
  } catch (error) {
    throw new Error(`Personalization is not valid JSON: ${String(error)}`);
  }
  const issues: string[] = [];
  if (!isRecord(value)) {
    throw new Error("Personalization must be an object");
  }
  if (value.schemaVersion !== 1) issues.push("schemaVersion must equal 1");
  if (typeof value.viewId !== "string") issues.push("viewId must be a string");
  if (!Number.isInteger(value.baseDefinitionVersion) || Number(value.baseDefinitionVersion) < 1) {
    issues.push("baseDefinitionVersion must be a positive integer");
  }
  if (!isRecord(value.defaultSlotOverrides)) {
    issues.push("defaultSlotOverrides must be an object");
  } else {
    Object.entries(value.defaultSlotOverrides).forEach(([slotId, override]) => {
      if (!isRecord(override) || typeof override.instanceId !== "string") {
        issues.push(`defaultSlotOverrides.${slotId} must contain an instanceId`);
      } else if (override.layout !== undefined) {
        if (!isRecord(override.layout)) {
          issues.push(`defaultSlotOverrides.${slotId}.layout must be an object`);
        } else {
          validateLayoutRecord(
            override.layout,
            override.instanceId,
            `defaultSlotOverrides.${slotId}.layout`,
            issues,
          );
        }
      }
    });
  }
  for (const field of ["addedInstances", "orphanedInstances"] as const) {
    const instances = value[field];
    if (!Array.isArray(instances)) issues.push(`${field} must be an array`);
    else instances.forEach((instance, index) => validatePersonalInstance(instance, `${field}[${index}]`, issues));
  }
  if (
    value.mobileOrderOverride !== null &&
    (!Array.isArray(value.mobileOrderOverride) ||
      value.mobileOrderOverride.some((id) => typeof id !== "string"))
  ) {
    issues.push("mobileOrderOverride must be null or an array of instance IDs");
  }
  if (issues.length > 0) throw new Error(issues.join("; "));
  return value as unknown as ViewPersonalizationPatch;
}

export class LocalStoragePersonalizationRepository implements PersonalizationRepository {
  readonly #storage: Storage;
  readonly #prefix: string;

  constructor(
    storage: Storage,
    prefix = "work-buddy.dashboard.personalization.v1",
  ) {
    this.#storage = storage;
    this.#prefix = prefix;
  }

  #key(viewId: ViewId): string {
    return `${this.#prefix}:${encodeURIComponent(viewId)}`;
  }

  async load(viewId: ViewId): Promise<ViewPersonalizationPatch | null> {
    const key = this.#key(viewId);
    const raw = this.#storage.getItem(key);
    if (raw === null) return null;
    try {
      const patch = parsePersonalizationPatch(raw);
      if (patch.viewId !== viewId) {
        throw new Error(`Stored personalization belongs to ${patch.viewId}`);
      }
      return patch;
    } catch (error) {
      throw new PersonalizationRepositoryError(String(error), key, raw);
    }
  }

  async save(patch: ViewPersonalizationPatch): Promise<void> {
    const key = this.#key(patch.viewId);
    const serialized = JSON.stringify(patch);
    try {
      parsePersonalizationPatch(serialized);
      this.#storage.setItem(key, serialized);
    } catch (error) {
      throw new PersonalizationRepositoryError(String(error), key, serialized);
    }
  }

  async reset(viewId: ViewId): Promise<void> {
    this.#storage.removeItem(this.#key(viewId));
  }
}

export class InMemoryPersonalizationRepository implements PersonalizationRepository {
  readonly #patches = new Map<ViewId, ViewPersonalizationPatch>();

  async load(viewId: ViewId): Promise<ViewPersonalizationPatch | null> {
    const patch = this.#patches.get(viewId);
    return patch === undefined ? null : structuredClone(patch);
  }

  async save(patch: ViewPersonalizationPatch): Promise<void> {
    parsePersonalizationPatch(JSON.stringify(patch));
    this.#patches.set(patch.viewId, structuredClone(patch));
  }

  async reset(viewId: ViewId): Promise<void> {
    this.#patches.delete(viewId);
  }
}
