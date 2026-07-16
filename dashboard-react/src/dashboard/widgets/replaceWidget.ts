import type {
  DefaultWidgetSlot,
  JsonValue,
  ViewDefinition,
  WidgetDefinition,
  WidgetTypeId,
} from "../contributions/contracts";
import {
  type ContributionRegistry,
  type RegisteredWidget,
} from "../contributions/registry";
import { widgetSatisfiesSlotRole } from "../contributions/validate";
import type {
  EffectiveWidgetInstance,
  ViewEditAction,
} from "../personalization/contracts";
import {
  executeWidgetMigration,
  planWidgetMigration,
  type MigratableWidgetState,
  type WidgetMigrationPlan,
  type WidgetMigrationResult,
  type WidgetMigrationStep,
} from "../personalization/migrations";

const slotForInstance = (
  view: ViewDefinition,
  instance: EffectiveWidgetInstance,
): DefaultWidgetSlot | undefined =>
  instance.slotId === undefined
    ? undefined
    : view.defaultSlots.find((slot) => slot.slotId === instance.slotId);

const acceptsCurrentSize = (
  definition: WidgetDefinition,
  instance: EffectiveWidgetInstance,
): boolean => {
  const { min, max } = definition.sizeContract;
  return (
    instance.layout.w >= min.w &&
    instance.layout.h >= min.h &&
    (max === undefined ||
      (instance.layout.w <= max.w && instance.layout.h <= max.h))
  );
};

const roleCompatible = (
  definition: WidgetDefinition,
  view: ViewDefinition,
  instance: EffectiveWidgetInstance,
): boolean => {
  const slot = slotForInstance(view, instance);
  if (slot !== undefined) return widgetSatisfiesSlotRole(definition, slot);
  return (
    instance.roleCompatibilityVersion !== undefined &&
    definition.providesRoles.includes(instance.roleCompatibilityVersion)
  );
};

/** Catalog filter used for both ordinary replacement and unavailable/orphan recovery. */
export function findCompatibleWidgetReplacements(
  registry: ContributionRegistry,
  view: ViewDefinition,
  instance: EffectiveWidgetInstance,
): readonly RegisteredWidget[] {
  return registry
    .listWidgets()
    .filter((candidate) => candidate.definition.typeId !== instance.widgetTypeId)
    .filter((candidate) => roleCompatible(candidate.definition, view, instance))
    .filter((candidate) => acceptsCurrentSize(candidate.definition, instance))
    .sort(
      (left, right) =>
        left.definition.libraryPath.join("/").localeCompare(
          right.definition.libraryPath.join("/"),
        ) || left.definition.displayName.localeCompare(right.definition.displayName),
    );
}

export interface ReplacementRequest {
  readonly registry: ContributionRegistry;
  readonly view: ViewDefinition;
  readonly instance: EffectiveWidgetInstance;
  readonly targetTypeId: WidgetTypeId;
  readonly migrations: readonly WidgetMigrationStep[];
  readonly targetDefaults?: MigratableWidgetState;
  readonly targetBindingVersion?: number;
  /** The UI must make this reset explicit before setting it true. */
  readonly allowExplicitReset: boolean;
}

export interface WidgetReplacementPlan {
  readonly instance: EffectiveWidgetInstance;
  readonly target: RegisteredWidget;
  readonly migrationPlan: WidgetMigrationPlan;
  readonly migrationResult: Extract<WidgetMigrationResult, { readonly ok: true }>;
  readonly action: Extract<ViewEditAction, { readonly type: "replace-widget" }>;
  readonly preserved: {
    readonly instanceId: EffectiveWidgetInstance["instanceId"];
    readonly slotId?: EffectiveWidgetInstance["slotId"];
    readonly layout: EffectiveWidgetInstance["layout"];
  };
}

export type WidgetReplacementPlanningResult =
  | { readonly ok: true; readonly plan: WidgetReplacementPlan }
  | {
      readonly ok: false;
      readonly reason:
        | "target-unavailable"
        | "same-type"
        | "role-incompatible"
        | "size-incompatible"
        | "migration-failed";
      readonly message: string;
      readonly migrationResult?: Extract<WidgetMigrationResult, { readonly ok: false }>;
    };

export function planWidgetReplacement(
  request: ReplacementRequest,
): WidgetReplacementPlanningResult {
  const target = request.registry.getWidget(request.targetTypeId);
  if (target === undefined) {
    return {
      ok: false,
      reason: "target-unavailable",
      message: `Widget type ${request.targetTypeId} is not installed.`,
    };
  }
  if (target.definition.typeId === request.instance.widgetTypeId) {
    return {
      ok: false,
      reason: "same-type",
      message: "The selected widget is already installed in this slot.",
    };
  }
  if (!roleCompatible(target.definition, request.view, request.instance)) {
    return {
      ok: false,
      reason: "role-incompatible",
      message: `${target.definition.displayName} does not satisfy this slot's role contract.`,
    };
  }
  if (!acceptsCurrentSize(target.definition, request.instance)) {
    return {
      ok: false,
      reason: "size-incompatible",
      message: `${target.definition.displayName} cannot preserve the current widget size.`,
    };
  }

  const slot = slotForInstance(request.view, request.instance);
  const migrationPlan = planWidgetMigration(
    {
      source: {
        widgetTypeId: request.instance.widgetTypeId,
        widgetDefinitionVersion: request.instance.widgetDefinitionVersion,
        settingsSchemaVersion: request.instance.settingsSchemaVersion,
        bindingVersion: request.instance.bindingVersion,
      },
      target: {
        widgetTypeId: target.definition.typeId,
        widgetDefinitionVersion: target.definition.definitionVersion,
        settingsSchemaVersion: target.definition.settingsSchema.version,
        bindingVersion: request.targetBindingVersion ?? 1,
      },
      sourceState: {
        settings: request.instance.settings,
        bindings: request.instance.bindings,
      },
      targetDefaults: request.targetDefaults ?? { settings: {}, bindings: {} },
      allowExplicitReset: request.allowExplicitReset,
    },
    request.migrations,
  );
  const migrationResult = executeWidgetMigration(migrationPlan);
  if (!migrationResult.ok) {
    return {
      ok: false,
      reason: "migration-failed",
      message: migrationResult.error,
      migrationResult,
    };
  }

  const action = {
    type: "replace-widget",
    instanceId: request.instance.instanceId,
    replacement: {
      widgetTypeId: target.definition.typeId,
      widgetDefinitionVersion: target.definition.definitionVersion,
      roleCompatibilityVersion:
        slot?.requiredRole ??
        request.instance.roleCompatibilityVersion ??
        target.definition.providesRoles[0]!,
    },
    settings: migrationResult.state.settings as JsonValue,
    settingsSchemaVersion: migrationResult.version.settingsSchemaVersion,
    bindings: migrationResult.state.bindings,
    bindingVersion: migrationResult.version.bindingVersion,
    roleCompatible: true,
  } as const satisfies Extract<ViewEditAction, { readonly type: "replace-widget" }>;
  return {
    ok: true,
    plan: {
      instance: request.instance,
      target,
      migrationPlan,
      migrationResult,
      action,
      preserved: {
        instanceId: request.instance.instanceId,
        ...(request.instance.slotId === undefined
          ? {}
          : { slotId: request.instance.slotId }),
        layout: request.instance.layout,
      },
    },
  };
}
