import type {
  JsonValue,
  WidgetTypeId,
} from "../contributions/contracts";
import { validateJsonValue } from "../contributions/validate";

export interface WidgetStateVersion {
  readonly widgetTypeId: WidgetTypeId;
  readonly widgetDefinitionVersion: number;
  readonly settingsSchemaVersion: number;
  readonly bindingVersion: number;
}

export interface MigratableWidgetState {
  readonly settings: JsonValue;
  readonly bindings: Readonly<Record<string, JsonValue>>;
}

export interface WidgetMigrationStep {
  readonly id: string;
  readonly from: WidgetStateVersion;
  readonly to: WidgetStateVersion;
  readonly description: string;
  migrate(state: MigratableWidgetState): MigratableWidgetState;
}

export interface WidgetMigrationRequest {
  readonly source: WidgetStateVersion;
  readonly target: WidgetStateVersion;
  readonly sourceState: MigratableWidgetState;
  readonly targetDefaults: MigratableWidgetState;
  readonly allowExplicitReset: boolean;
}

export type WidgetMigrationPlan =
  | {
      readonly mode: "preserve";
      readonly request: WidgetMigrationRequest;
      readonly steps: readonly [];
      readonly warnings: readonly string[];
    }
  | {
      readonly mode: "migrate";
      readonly request: WidgetMigrationRequest;
      readonly steps: readonly WidgetMigrationStep[];
      readonly warnings: readonly string[];
    }
  | {
      readonly mode: "reset";
      readonly request: WidgetMigrationRequest;
      readonly steps: readonly [];
      readonly warnings: readonly string[];
    }
  | {
      readonly mode: "unavailable";
      readonly request: WidgetMigrationRequest;
      readonly steps: readonly [];
      readonly warnings: readonly string[];
      readonly reason: string;
    };

export type WidgetMigrationResult =
  | {
      readonly ok: true;
      readonly mode: Exclude<WidgetMigrationPlan["mode"], "unavailable">;
      readonly version: WidgetStateVersion;
      readonly state: MigratableWidgetState;
      readonly appliedStepIds: readonly string[];
      readonly warnings: readonly string[];
      readonly recoverableSource?: MigratableWidgetState;
    }
  | {
      readonly ok: false;
      readonly error: string;
      readonly failedStepId?: string;
      readonly recoverableSource: MigratableWidgetState;
    };

const versionKey = (version: WidgetStateVersion): string =>
  [
    version.widgetTypeId,
    version.widgetDefinitionVersion,
    version.settingsSchemaVersion,
    version.bindingVersion,
  ].join("@");

const validateState = (state: MigratableWidgetState): readonly string[] => [
  ...validateJsonValue(state.settings, "settings").map((issue) => issue.message),
  ...validateJsonValue(state.bindings, "bindings").map((issue) => issue.message),
];

/** Deterministic shortest-path planning over explicitly registered migrations. */
export function planWidgetMigration(
  request: WidgetMigrationRequest,
  migrations: readonly WidgetMigrationStep[],
): WidgetMigrationPlan {
  const sourceKey = versionKey(request.source);
  const targetKey = versionKey(request.target);
  if (sourceKey === targetKey) {
    return { mode: "preserve", request, steps: [], warnings: [] };
  }

  const queue: Array<{
    readonly version: WidgetStateVersion;
    readonly steps: readonly WidgetMigrationStep[];
  }> = [{ version: request.source, steps: [] }];
  const visited = new Set([sourceKey]);
  while (queue.length > 0) {
    const current = queue.shift()!;
    const outgoing = migrations.filter(
      (migration) => versionKey(migration.from) === versionKey(current.version),
    );
    for (const migration of outgoing) {
      const nextSteps = [...current.steps, migration];
      const nextKey = versionKey(migration.to);
      if (nextKey === targetKey) {
        return {
          mode: "migrate",
          request,
          steps: nextSteps,
          warnings: nextSteps.map((step) => step.description),
        };
      }
      if (!visited.has(nextKey)) {
        visited.add(nextKey);
        queue.push({ version: migration.to, steps: nextSteps });
      }
    }
  }

  if (request.allowExplicitReset) {
    return {
      mode: "reset",
      request,
      steps: [],
      warnings: [
        "No compatible field migration exists; target defaults will be used and prior state retained for recovery.",
      ],
    };
  }
  return {
    mode: "unavailable",
    request,
    steps: [],
    warnings: [],
    reason: "No registered migration reaches the requested widget/schema versions.",
  };
}

export function executeWidgetMigration(plan: WidgetMigrationPlan): WidgetMigrationResult {
  const source = structuredClone(plan.request.sourceState);
  if (plan.mode === "unavailable") {
    return { ok: false, error: plan.reason, recoverableSource: source };
  }
  if (plan.mode === "reset") {
    const resetState = structuredClone(plan.request.targetDefaults);
    const issues = validateState(resetState);
    return issues.length > 0
      ? {
          ok: false,
          error: `Target defaults are invalid: ${issues.join("; ")}`,
          recoverableSource: source,
        }
      : {
          ok: true,
          mode: "reset",
          version: plan.request.target,
          state: resetState,
          appliedStepIds: [],
          warnings: plan.warnings,
          recoverableSource: source,
        };
  }
  if (plan.mode === "preserve") {
    const issues = validateState(source);
    return issues.length > 0
      ? {
          ok: false,
          error: `Source widget state is invalid: ${issues.join("; ")}`,
          recoverableSource: source,
        }
      : {
          ok: true,
          mode: "preserve",
          version: plan.request.target,
          state: source,
          appliedStepIds: [],
          warnings: plan.warnings,
        };
  }

  let state = source;
  for (const step of plan.steps) {
    try {
      state = step.migrate(structuredClone(state));
      const issues = validateState(state);
      if (issues.length > 0) throw new Error(issues.join("; "));
    } catch (error) {
      return {
        ok: false,
        error: `Migration ${step.id} failed: ${String(error)}`,
        failedStepId: step.id,
        recoverableSource: source,
      };
    }
  }
  return {
    ok: true,
    mode: "migrate",
    version: plan.request.target,
    state,
    appliedStepIds: plan.steps.map((step) => step.id),
    warnings: plan.warnings,
  };
}
