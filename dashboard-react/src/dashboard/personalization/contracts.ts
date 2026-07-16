import type {
  JsonObject,
  JsonValue,
  ViewId,
  WidgetInstanceId,
  WidgetRoleId,
  WidgetSlotId,
  WidgetTypeId,
} from "../contributions/contracts";
import type { DashboardLayout, LayoutCommand, WidgetLayoutItem } from "../layout/contracts";

export interface SelectedWidgetType {
  readonly widgetTypeId: WidgetTypeId;
  readonly widgetDefinitionVersion: number;
  readonly roleCompatibilityVersion: WidgetRoleId;
}

export interface DefaultSlotOverride {
  readonly slotId: WidgetSlotId;
  readonly instanceId: WidgetInstanceId;
  readonly selectedWidget?: SelectedWidgetType;
  readonly layout?: WidgetLayoutItem;
  readonly visibility?: "shown" | "hidden";
  /** Field-level delta over the current widget settings defaults. */
  readonly settingsPatch?: JsonObject;
  readonly settingsReplacement?: JsonValue;
  readonly settingsSchemaVersion?: number;
  readonly bindings?: Readonly<Record<string, JsonValue>>;
  readonly bindingVersion?: number;
  readonly migrationFallback?: JsonValue;
}

export interface PersonalWidgetInstance {
  readonly instanceId: WidgetInstanceId;
  readonly formerSlotId?: WidgetSlotId;
  readonly widgetTypeId: WidgetTypeId;
  readonly widgetDefinitionVersion: number;
  readonly roleCompatibilityVersion?: WidgetRoleId;
  readonly settings: JsonValue;
  readonly settingsSchemaVersion: number;
  readonly bindings: Readonly<Record<string, JsonValue>>;
  readonly bindingVersion: number;
  readonly visibility: "shown" | "hidden";
  readonly layout: WidgetLayoutItem;
  readonly unavailableReason?: string;
}

/** Portable user-owned delta. It deliberately contains no RGL `i`, static, or breakpoint maps. */
export interface ViewPersonalizationPatch {
  readonly schemaVersion: 1;
  readonly viewId: ViewId;
  readonly baseDefinitionVersion: number;
  readonly defaultSlotOverrides: Readonly<Record<string, DefaultSlotOverride>>;
  readonly addedInstances: readonly PersonalWidgetInstance[];
  readonly orphanedInstances: readonly PersonalWidgetInstance[];
  readonly mobileOrderOverride: readonly WidgetInstanceId[] | null;
}

export interface EffectiveWidgetInstance {
  readonly instanceId: WidgetInstanceId;
  readonly slotId?: WidgetSlotId;
  readonly widgetTypeId: WidgetTypeId;
  readonly widgetDefinitionVersion: number;
  readonly roleCompatibilityVersion?: WidgetRoleId;
  readonly settings: JsonValue;
  readonly settingsSchemaVersion: number;
  readonly bindings: Readonly<Record<string, JsonValue>>;
  readonly bindingVersion: number;
  readonly visibility: "shown" | "hidden";
  readonly presence: "required" | "default_on" | "default_off" | "personal";
  readonly layout: WidgetLayoutItem;
  readonly unavailableReason?: string;
}

export interface ViewEditSnapshot {
  readonly instances: readonly EffectiveWidgetInstance[];
  readonly mobileOrder: readonly WidgetInstanceId[];
}

export interface ViewEditSessionState {
  readonly opening: ViewEditSnapshot;
  readonly present: ViewEditSnapshot;
  readonly past: readonly ViewEditSnapshot[];
  readonly future: readonly ViewEditSnapshot[];
  readonly interactionOrigin?: ViewEditSnapshot;
  readonly status: "editing" | "done" | "cancelled";
  readonly dirty: boolean;
  readonly lastFailure?: string;
}

export type ViewEditAction =
  | { readonly type: "begin-interaction" }
  | { readonly type: "preview-layout"; readonly layout: DashboardLayout }
  | { readonly type: "commit-interaction" }
  | { readonly type: "cancel-interaction" }
  | { readonly type: "layout-command"; readonly command: LayoutCommand }
  | { readonly type: "add"; readonly instance: EffectiveWidgetInstance }
  | { readonly type: "hide"; readonly instanceId: WidgetInstanceId }
  | { readonly type: "show"; readonly instanceId: WidgetInstanceId }
  | { readonly type: "remove"; readonly instanceId: WidgetInstanceId }
  | {
      readonly type: "replace-widget";
      readonly instanceId: WidgetInstanceId;
      readonly replacement: SelectedWidgetType;
      readonly settings: JsonValue;
      readonly settingsSchemaVersion: number;
      readonly bindings: Readonly<Record<string, JsonValue>>;
      readonly bindingVersion: number;
      readonly roleCompatible: boolean;
    }
  | {
      readonly type: "configure";
      readonly instanceId: WidgetInstanceId;
      readonly settings: JsonValue;
      readonly settingsSchemaVersion: number;
    }
  | { readonly type: "set-mobile-order"; readonly order: readonly WidgetInstanceId[] }
  | { readonly type: "tidy" }
  | { readonly type: "undo" }
  | { readonly type: "redo" }
  | { readonly type: "cancel" }
  | { readonly type: "done" }
  | { readonly type: "reset"; readonly defaults: ViewEditSnapshot };
