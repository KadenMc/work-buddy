import type {
  DefaultWidgetSlot,
  JsonObject,
  JsonValue,
  ViewDefinition,
  WidgetDefinition,
  WidgetInstanceId,
  WidgetTypeId,
} from "../contributions/contracts";
import { deriveMobileOrder } from "../layout/mobileOrder";
import {
  addLayoutItem,
  applyLayoutCommand,
  tidyDashboardLayout,
  validateDashboardLayout,
} from "../layout/operations";
import type { DashboardLayout } from "../layout/contracts";
import type {
  DefaultSlotOverride,
  EffectiveWidgetInstance,
  PersonalWidgetInstance,
  ViewEditAction,
  ViewEditSessionState,
  ViewEditSnapshot,
  ViewPersonalizationPatch,
} from "./contracts";

const HISTORY_LIMIT = 100;

const snapshotsEqual = (left: ViewEditSnapshot, right: ViewEditSnapshot): boolean =>
  JSON.stringify(left) === JSON.stringify(right);

const visibleLayout = (snapshot: ViewEditSnapshot): DashboardLayout =>
  snapshot.instances
    .filter((instance) => instance.visibility === "shown")
    .map((instance) => instance.layout);

const withLayout = (
  snapshot: ViewEditSnapshot,
  layout: DashboardLayout,
): ViewEditSnapshot => {
  const byId = new Map(layout.map((item) => [item.instanceId, item]));
  return {
    ...snapshot,
    instances: snapshot.instances.map((instance) => ({
      ...instance,
      layout: byId.get(instance.instanceId) ?? instance.layout,
    })),
  };
};

const complete = (
  state: ViewEditSessionState,
  present: ViewEditSnapshot,
  past = [...state.past, state.present].slice(-HISTORY_LIMIT),
): ViewEditSessionState =>
  snapshotsEqual(state.present, present)
    ? { ...state, interactionOrigin: undefined, lastFailure: undefined }
    : {
        ...state,
        present,
        past,
        future: [],
        interactionOrigin: undefined,
        dirty: !snapshotsEqual(state.opening, present),
        lastFailure: undefined,
      };

const fail = (state: ViewEditSessionState, message: string): ViewEditSessionState => ({
  ...state,
  lastFailure: message,
});

const updateInstance = (
  snapshot: ViewEditSnapshot,
  instanceId: WidgetInstanceId,
  update: (instance: EffectiveWidgetInstance) => EffectiveWidgetInstance,
): ViewEditSnapshot => ({
  ...snapshot,
  instances: snapshot.instances.map((instance) =>
    instance.instanceId === instanceId ? update(instance) : instance,
  ),
});

const findInstance = (
  snapshot: ViewEditSnapshot,
  instanceId: WidgetInstanceId,
): EffectiveWidgetInstance | undefined =>
  snapshot.instances.find((instance) => instance.instanceId === instanceId);

export const beginViewEditSession = (
  opening: ViewEditSnapshot,
): ViewEditSessionState => ({
  opening,
  present: opening,
  past: [],
  future: [],
  status: "editing",
  dirty: false,
});

export function viewEditSessionReducer(
  state: ViewEditSessionState,
  action: ViewEditAction,
): ViewEditSessionState {
  if (state.status !== "editing") return state;

  switch (action.type) {
    case "begin-interaction":
      return state.interactionOrigin === undefined
        ? { ...state, interactionOrigin: state.present, lastFailure: undefined }
        : state;
    case "preview-layout": {
      const issues = validateDashboardLayout(action.layout);
      return issues.length > 0
        ? fail(state, issues[0]!)
        : {
            ...state,
            present: withLayout(state.present, action.layout),
            dirty: !snapshotsEqual(state.opening, withLayout(state.present, action.layout)),
            lastFailure: undefined,
          };
    }
    case "commit-interaction": {
      const origin = state.interactionOrigin;
      if (origin === undefined) return state;
      if (snapshotsEqual(origin, state.present)) {
        return { ...state, interactionOrigin: undefined };
      }
      return {
        ...state,
        past: [...state.past, origin].slice(-HISTORY_LIMIT),
        future: [],
        interactionOrigin: undefined,
        dirty: !snapshotsEqual(state.opening, state.present),
      };
    }
    case "cancel-interaction":
      return state.interactionOrigin === undefined
        ? state
        : {
            ...state,
            present: state.interactionOrigin,
            interactionOrigin: undefined,
            dirty: !snapshotsEqual(state.opening, state.interactionOrigin),
          };
    case "layout-command": {
      const result = applyLayoutCommand(visibleLayout(state.present), action.command);
      return result.accepted
        ? complete(state, withLayout(state.present, result.items))
        : fail(state, result.reason ?? "Layout command rejected");
    }
    case "add": {
      if (findInstance(state.present, action.instance.instanceId) !== undefined) {
        return fail(state, `Instance ${action.instance.instanceId} already exists`);
      }
      const result = addLayoutItem(visibleLayout(state.present), action.instance.layout);
      if (!result.accepted) return fail(state, result.reason ?? "Add rejected");
      const placed = result.items.find(
        (item) => item.instanceId === action.instance.instanceId,
      )!;
      const present: ViewEditSnapshot = {
        instances: [...state.present.instances, { ...action.instance, layout: placed }],
        mobileOrder:
          action.instance.visibility === "shown"
            ? [...state.present.mobileOrder, action.instance.instanceId]
            : state.present.mobileOrder,
      };
      return complete(state, present);
    }
    case "hide": {
      const instance = findInstance(state.present, action.instanceId);
      if (instance === undefined) return fail(state, "Widget instance not found");
      if (instance.presence === "required") {
        return fail(state, "A required view purpose cannot be hidden");
      }
      if (instance.visibility === "hidden") return state;
      const present = updateInstance(state.present, action.instanceId, (current) => ({
        ...current,
        visibility: "hidden",
      }));
      return complete(state, {
        ...present,
        mobileOrder: present.mobileOrder.filter((id) => id !== action.instanceId),
      });
    }
    case "show": {
      const instance = findInstance(state.present, action.instanceId);
      if (instance === undefined) return fail(state, "Widget instance not found");
      if (instance.visibility === "shown") return state;
      const result = addLayoutItem(visibleLayout(state.present), instance.layout);
      if (!result.accepted) return fail(state, result.reason ?? "Show rejected");
      const placed = result.items.find((item) => item.instanceId === instance.instanceId)!;
      const present = updateInstance(state.present, action.instanceId, (current) => ({
        ...current,
        visibility: "shown",
        layout: placed,
      }));
      return complete(state, {
        ...present,
        mobileOrder: [...present.mobileOrder, action.instanceId],
      });
    }
    case "remove": {
      const instance = findInstance(state.present, action.instanceId);
      if (instance === undefined) return fail(state, "Widget instance not found");
      if (instance.presence === "required") {
        return fail(state, "A required view purpose cannot be removed");
      }
      if (instance.slotId !== undefined) {
        return viewEditSessionReducer(state, { type: "hide", instanceId: action.instanceId });
      }
      return complete(state, {
        instances: state.present.instances.filter(
          (candidate) => candidate.instanceId !== action.instanceId,
        ),
        mobileOrder: state.present.mobileOrder.filter((id) => id !== action.instanceId),
      });
    }
    case "replace-widget": {
      const instance = findInstance(state.present, action.instanceId);
      if (instance === undefined) return fail(state, "Widget instance not found");
      if (!action.roleCompatible) {
        return fail(state, "Replacement does not satisfy the slot role");
      }
      return complete(
        state,
        updateInstance(state.present, action.instanceId, (current) => ({
          ...current,
          widgetTypeId: action.replacement.widgetTypeId,
          widgetDefinitionVersion: action.replacement.widgetDefinitionVersion,
          roleCompatibilityVersion: action.replacement.roleCompatibilityVersion,
          settings: action.settings,
          settingsSchemaVersion: action.settingsSchemaVersion,
          bindings: action.bindings,
          bindingVersion: action.bindingVersion,
        })),
      );
    }
    case "configure": {
      if (findInstance(state.present, action.instanceId) === undefined) {
        return fail(state, "Widget instance not found");
      }
      return complete(
        state,
        updateInstance(state.present, action.instanceId, (current) => ({
          ...current,
          settings: action.settings,
          settingsSchemaVersion: action.settingsSchemaVersion,
        })),
      );
    }
    case "set-mobile-order": {
      const visibleIds = state.present.instances
        .filter((instance) => instance.visibility === "shown")
        .map((instance) => instance.instanceId);
      if (
        action.order.length !== visibleIds.length ||
        new Set(action.order).size !== action.order.length ||
        visibleIds.some((id) => !action.order.includes(id))
      ) {
        return fail(state, "Mobile order must contain every shown widget exactly once");
      }
      return complete(state, { ...state.present, mobileOrder: [...action.order] });
    }
    case "tidy":
      return complete(state, withLayout(state.present, tidyDashboardLayout(visibleLayout(state.present))));
    case "undo": {
      const previous = state.past[state.past.length - 1];
      if (previous === undefined) return state;
      return {
        ...state,
        present: previous,
        past: state.past.slice(0, -1),
        future: [state.present, ...state.future],
        interactionOrigin: undefined,
        dirty: !snapshotsEqual(state.opening, previous),
        lastFailure: undefined,
      };
    }
    case "redo": {
      const next = state.future[0];
      if (next === undefined) return state;
      return {
        ...state,
        present: next,
        past: [...state.past, state.present].slice(-HISTORY_LIMIT),
        future: state.future.slice(1),
        interactionOrigin: undefined,
        dirty: !snapshotsEqual(state.opening, next),
        lastFailure: undefined,
      };
    }
    case "cancel":
      return {
        ...state,
        present: state.opening,
        past: [],
        future: [],
        interactionOrigin: undefined,
        status: "cancelled",
        dirty: false,
        lastFailure: undefined,
      };
    case "done":
      return {
        ...state,
        opening: state.present,
        past: [],
        future: [],
        interactionOrigin: undefined,
        status: "done",
        dirty: false,
        lastFailure: undefined,
      };
    case "reset":
      return complete(state, action.defaults);
  }
}

const isJsonObject = (value: JsonValue): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const mergeSettings = (defaults: JsonValue, patch?: JsonObject): JsonValue =>
  patch === undefined
    ? defaults
    : { ...(isJsonObject(defaults) ? defaults : {}), ...patch };

const widgetDefinition = (
  definitions: ReadonlyMap<WidgetTypeId, WidgetDefinition>,
  typeId: WidgetTypeId,
): WidgetDefinition | undefined => definitions.get(typeId);

const defaultInstance = (
  slot: DefaultWidgetSlot,
  definitions: ReadonlyMap<WidgetTypeId, WidgetDefinition>,
): EffectiveWidgetInstance => {
  const definition = widgetDefinition(definitions, slot.defaultWidgetTypeId);
  return {
    instanceId: slot.defaultInstanceId,
    slotId: slot.slotId,
    widgetTypeId: slot.defaultWidgetTypeId,
    widgetDefinitionVersion: definition?.definitionVersion ?? 1,
    roleCompatibilityVersion: slot.requiredRole,
    settings: slot.defaultSettings,
    settingsSchemaVersion: definition?.settingsSchema.version ?? 1,
    bindings: slot.defaultBindings ?? {},
    bindingVersion: 1,
    visibility: slot.presence === "default_off" ? "hidden" : "shown",
    presence: slot.presence,
    layout: {
      instanceId: slot.defaultInstanceId,
      ...slot.defaultLayout,
      ...(definition === undefined
        ? {}
        : {
            minW: definition.sizeContract.min.w,
            minH: definition.sizeContract.min.h,
            ...(definition.sizeContract.max === undefined
              ? {}
              : {
                  maxW: definition.sizeContract.max.w,
                  maxH: definition.sizeContract.max.h,
                }),
          }),
    },
    ...(definition === undefined
      ? { unavailableReason: `Widget type ${slot.defaultWidgetTypeId} is unavailable` }
      : {}),
  };
};

const effectiveFromPersonal = (instance: PersonalWidgetInstance): EffectiveWidgetInstance => ({
  ...instance,
  ...(instance.formerSlotId === undefined ? {} : { slotId: instance.formerSlotId }),
  presence: "personal",
});

export function resolveViewPersonalization(
  view: ViewDefinition,
  definitions: ReadonlyMap<WidgetTypeId, WidgetDefinition>,
  patch?: ViewPersonalizationPatch,
): ViewEditSnapshot {
  if (patch !== undefined && patch.viewId !== view.viewId) {
    throw new Error(`Personalization for ${patch.viewId} cannot be applied to ${view.viewId}`);
  }
  const overrides = patch?.defaultSlotOverrides ?? {};
  const instances = view.defaultSlots.map((slot) => {
    const base = defaultInstance(slot, definitions);
    const override = overrides[slot.slotId];
    if (override === undefined) return base;
    const selected = override.selectedWidget;
    const definition = widgetDefinition(
      definitions,
      selected?.widgetTypeId ?? base.widgetTypeId,
    );
    return {
      ...base,
      instanceId: override.instanceId,
      widgetTypeId: selected?.widgetTypeId ?? base.widgetTypeId,
      widgetDefinitionVersion:
        selected?.widgetDefinitionVersion ?? base.widgetDefinitionVersion,
      roleCompatibilityVersion:
        selected?.roleCompatibilityVersion ?? base.roleCompatibilityVersion,
      settings:
        override.settingsReplacement !== undefined
          ? override.settingsReplacement
          : mergeSettings(base.settings, override.settingsPatch),
      settingsSchemaVersion:
        override.settingsSchemaVersion ?? definition?.settingsSchema.version ?? base.settingsSchemaVersion,
      bindings: override.bindings ?? base.bindings,
      bindingVersion: override.bindingVersion ?? base.bindingVersion,
      visibility:
        slot.presence === "required" ? "shown" : override.visibility ?? base.visibility,
      layout: override.layout ?? { ...base.layout, instanceId: override.instanceId },
      ...(definition === undefined
        ? { unavailableReason: `Widget type ${selected?.widgetTypeId ?? base.widgetTypeId} is unavailable` }
        : { unavailableReason: undefined }),
    } satisfies EffectiveWidgetInstance;
  });
  instances.push(
    ...(patch?.addedInstances ?? []).map(effectiveFromPersonal),
    ...(patch?.orphanedInstances ?? []).map(effectiveFromPersonal),
  );
  const preferredSlots = view.mobileOrder.length > 0 ? view.mobileOrder : view.readingOrder;
  const mobileOrder = deriveMobileOrder(
    instances.map((instance) => ({
      instanceId: instance.instanceId,
      ...(instance.slotId === undefined ? {} : { slotId: instance.slotId }),
      visibility: instance.visibility,
      layout: instance.layout,
    })),
    preferredSlots,
    patch?.mobileOrderOverride ?? undefined,
  );
  return { instances, mobileOrder };
}

const jsonEqual = (left: unknown, right: unknown): boolean =>
  JSON.stringify(left) === JSON.stringify(right);

const settingsDelta = (
  defaults: JsonValue,
  current: JsonValue,
): { readonly patch?: JsonObject; readonly replacement?: JsonValue } => {
  if (!isJsonObject(defaults) || !isJsonObject(current)) {
    return jsonEqual(defaults, current) ? {} : { replacement: current };
  }
  if (Object.keys(defaults).some((key) => !(key in current))) {
    return { replacement: current };
  }
  const delta = Object.fromEntries(
    Object.entries(current).filter(([key, value]) => !jsonEqual(defaults[key], value)),
  ) as JsonObject;
  return Object.keys(delta).length === 0 ? {} : { patch: delta };
};

export function createPersonalizationPatch(
  view: ViewDefinition,
  definitions: ReadonlyMap<WidgetTypeId, WidgetDefinition>,
  snapshot: ViewEditSnapshot,
): ViewPersonalizationPatch {
  const defaultSlotOverrides: Record<string, DefaultSlotOverride> = {};
  const slotIds = new Set(view.defaultSlots.map((slot) => slot.slotId));

  view.defaultSlots.forEach((slot) => {
    const current = snapshot.instances.find((instance) => instance.slotId === slot.slotId);
    if (current === undefined) return;
    const base = defaultInstance(slot, definitions);
    const settingsChange = settingsDelta(base.settings, current.settings);
    const changedType =
      current.widgetTypeId !== base.widgetTypeId ||
      current.widgetDefinitionVersion !== base.widgetDefinitionVersion;
    const override: DefaultSlotOverride = {
      slotId: slot.slotId,
      instanceId: current.instanceId,
      ...(changedType
        ? {
            selectedWidget: {
              widgetTypeId: current.widgetTypeId,
              widgetDefinitionVersion: current.widgetDefinitionVersion,
              roleCompatibilityVersion:
                current.roleCompatibilityVersion ?? slot.requiredRole,
            },
          }
        : {}),
      ...(!jsonEqual(current.layout, base.layout) ? { layout: current.layout } : {}),
      ...(current.visibility !== base.visibility ? { visibility: current.visibility } : {}),
      ...(settingsChange.patch === undefined ? {} : { settingsPatch: settingsChange.patch }),
      ...(settingsChange.replacement === undefined
        ? {}
        : { settingsReplacement: settingsChange.replacement }),
      ...(current.settingsSchemaVersion !== base.settingsSchemaVersion
        ? { settingsSchemaVersion: current.settingsSchemaVersion }
        : {}),
      ...(!jsonEqual(current.bindings, base.bindings) ? { bindings: current.bindings } : {}),
      ...(current.bindingVersion !== base.bindingVersion
        ? { bindingVersion: current.bindingVersion }
        : {}),
    };
    if (Object.keys(override).length > 2) defaultSlotOverrides[slot.slotId] = override;
  });

  const toPersonal = (instance: EffectiveWidgetInstance): PersonalWidgetInstance => ({
    instanceId: instance.instanceId,
    ...(instance.slotId === undefined ? {} : { formerSlotId: instance.slotId }),
    widgetTypeId: instance.widgetTypeId,
    widgetDefinitionVersion: instance.widgetDefinitionVersion,
    ...(instance.roleCompatibilityVersion === undefined
      ? {}
      : { roleCompatibilityVersion: instance.roleCompatibilityVersion }),
    settings: instance.settings,
    settingsSchemaVersion: instance.settingsSchemaVersion,
    bindings: instance.bindings,
    bindingVersion: instance.bindingVersion,
    visibility: instance.visibility,
    layout: instance.layout,
    ...(instance.unavailableReason === undefined
      ? {}
      : { unavailableReason: instance.unavailableReason }),
  });
  const personal = snapshot.instances.filter(
    (instance) => instance.slotId === undefined || !slotIds.has(instance.slotId),
  );
  const addedInstances = personal
    .filter((instance) => instance.unavailableReason === undefined)
    .map(toPersonal);
  const orphanedInstances = personal
    .filter((instance) => instance.unavailableReason !== undefined)
    .map(toPersonal);
  const defaults = resolveViewPersonalization(view, definitions);
  return {
    schemaVersion: 1,
    viewId: view.viewId,
    baseDefinitionVersion: view.definitionVersion,
    defaultSlotOverrides,
    addedInstances,
    orphanedInstances,
    mobileOrderOverride: jsonEqual(snapshot.mobileOrder, defaults.mobileOrder)
      ? null
      : snapshot.mobileOrder,
  };
}
