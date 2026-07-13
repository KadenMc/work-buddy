import type {
  CanvasThemeSnapshot,
  ResolvedThemeSummary,
  WidgetThemeDeclaration,
} from "./themeContract";

export type JsonPrimitive = boolean | number | string | null;
export type JsonValue =
  | JsonPrimitive
  | { readonly [key: string]: JsonValue }
  | readonly JsonValue[];
export type JsonObject = { readonly [key: string]: JsonValue };

declare const dashboardIdBrand: unique symbol;
type DashboardId<Kind extends string> = string & {
  readonly [dashboardIdBrand]: Kind;
};

/** Distinct string identities remain ordinary strings on the wire. */
export type AppId = DashboardId<"app">;
export type ViewId = DashboardId<"view">;
export type WidgetTypeId = DashboardId<"widget-type">;
export type WidgetSlotId = DashboardId<"widget-slot">;
export type WidgetInstanceId = DashboardId<"widget-instance">;
export type WidgetRoleId = DashboardId<"widget-role">;
export type WidgetModuleId = DashboardId<"widget-module">;

export const asAppId = (value: string): AppId => value as AppId;
export const asViewId = (value: string): ViewId => value as ViewId;
export const asWidgetTypeId = (value: string): WidgetTypeId => value as WidgetTypeId;
export const asWidgetSlotId = (value: string): WidgetSlotId => value as WidgetSlotId;
export const asWidgetInstanceId = (value: string): WidgetInstanceId =>
  value as WidgetInstanceId;
export const asWidgetRoleId = (value: string): WidgetRoleId => value as WidgetRoleId;
export const asWidgetModuleId = (value: string): WidgetModuleId => value as WidgetModuleId;

export interface JsonSchemaReference {
  readonly schemaId: string;
  readonly version: number;
}

export interface WidgetRoleContract {
  readonly roleId: WidgetRoleId;
  readonly ownerAppId: AppId;
  readonly displayName: string;
  readonly description: string;
  readonly inputSchema?: JsonSchemaReference;
  readonly outputIntentSchemas?: readonly JsonSchemaReference[];
}

export interface RoleCompatibilityRule {
  /** Additional role contracts accepted in place of the required role. */
  readonly compatibleRoleIds?: readonly WidgetRoleId[];
  readonly allowedPublisherAppIds?: readonly AppId[];
  readonly minimumDefinitionVersion?: number;
}

export type WidgetSizeMode = "compact" | "standard" | "expanded";

export interface GridSize {
  readonly w: number;
  readonly h: number;
}

export interface WidgetSizeContract {
  readonly default: GridSize;
  readonly min: GridSize;
  readonly max?: GridSize;
  readonly modes: readonly WidgetSizeMode[];
}

export interface WidgetLayoutPlacement extends GridSize {
  readonly x: number;
  readonly y: number;
}

export interface DashboardLayoutItem extends WidgetLayoutPlacement {
  readonly instanceId: WidgetInstanceId;
}

export interface DashboardGridDefinition {
  readonly columns: number;
}

export type WidgetMultiplicity = "single_per_view" | "multiple_per_view";

/** A reusable library type. It never identifies a placement in a view. */
export interface WidgetDefinition {
  readonly typeId: WidgetTypeId;
  readonly definitionVersion: number;
  readonly publisherAppId: AppId;
  readonly displayName: string;
  readonly description: string;
  readonly libraryPath: readonly string[];
  readonly providesRoles: readonly WidgetRoleId[];
  readonly settingsSchema: JsonSchemaReference;
  readonly inputSchema: JsonSchemaReference;
  readonly outputIntentSchemas: readonly JsonSchemaReference[];
  readonly sizeContract: WidgetSizeContract;
  readonly multiplicity: WidgetMultiplicity;
  readonly rendererModuleId: WidgetModuleId;
  readonly theme: WidgetThemeDeclaration;
}

/** A stable purpose owned by a view, independent from its selected widget type. */
export interface DefaultWidgetSlot {
  readonly slotId: WidgetSlotId;
  readonly defaultInstanceId: WidgetInstanceId;
  readonly requiredRole: WidgetRoleId;
  readonly defaultWidgetTypeId: WidgetTypeId;
  readonly presence: "required" | "default_on" | "default_off";
  readonly defaultSettings: JsonValue;
  readonly defaultBindings?: Readonly<Record<string, JsonValue>>;
  readonly defaultLayout: WidgetLayoutPlacement;
  readonly allowedSubstitution?: RoleCompatibilityRule;
  readonly lockedReason?: string;
}

export interface ViewDefinition {
  readonly viewId: ViewId;
  readonly definitionVersion: number;
  readonly ownerAppId: AppId;
  readonly displayName: string;
  /** Route segment beneath the dashboard's `/app/` basename (for example `journal`). */
  readonly route: string;
  readonly navigation: {
    readonly label: string;
    readonly order: number;
    readonly isDefault?: boolean;
    readonly hidden?: boolean;
  };
  readonly primaryJob: string;
  readonly grid: DashboardGridDefinition;
  readonly defaultSlots: readonly DefaultWidgetSlot[];
  readonly readingOrder: readonly WidgetSlotId[];
  readonly mobileOrder: readonly WidgetSlotId[];
}

/** Pure contribution data; executable modules are registered through WidgetModule. */
export interface AppContribution {
  readonly schemaVersion: 1;
  readonly appId: AppId;
  readonly definitionVersion: number;
  readonly displayName: string;
  readonly widgetRoles: readonly WidgetRoleContract[];
  readonly widgetDefinitions: readonly WidgetDefinition[];
  readonly views: readonly ViewDefinition[];
}

export interface LoadedWidgetModule {
  /** The WidgetHost narrows this export to its React renderer contract when mounting. */
  readonly default: unknown;
}

/** Runtime-only lazy binding kept separate from JSON-compatible contribution data. */
export interface WidgetModule {
  readonly moduleId: WidgetModuleId;
  readonly widgetTypeId: WidgetTypeId;
  load(): Promise<LoadedWidgetModule>;
}

export interface WidgetInstance {
  readonly instanceId: WidgetInstanceId;
  readonly viewId: ViewId;
  readonly slotId?: WidgetSlotId;
  readonly widgetTypeId: WidgetTypeId;
  readonly widgetDefinitionVersion: number;
  readonly settings: JsonValue;
  readonly bindings: Readonly<Record<string, JsonValue>>;
  readonly visibility: "shown" | "hidden";
}

export type SnapshotRevision = string | number;
export type SnapshotStatus =
  | "ready"
  | "stale"
  | "offline"
  | "unavailable"
  | "permission-denied"
  | "read-only"
  | "error";

export interface SnapshotQuality {
  readonly kind: "complete" | "partial" | "demo";
  readonly message?: string;
}

export interface ViewSnapshot<
  Model = unknown,
  Binding = unknown,
  WidgetInput = unknown,
> {
  readonly viewId: ViewId;
  readonly revision?: SnapshotRevision;
  readonly observedAt: string;
  readonly status: SnapshotStatus;
  readonly quality: SnapshotQuality;
  readonly model: Model;
  readonly bindings: Readonly<Record<string, Binding>>;
  /** Keyed by opaque instance ID, never by type ID. */
  readonly widgetInputs: Readonly<Record<string, WidgetInput>>;
}

export interface WidgetSnapshot<Input = unknown> {
  readonly widgetTypeId: WidgetTypeId;
  readonly instanceId: WidgetInstanceId;
  readonly revision?: SnapshotRevision;
  readonly observedAt: string;
  readonly status: SnapshotStatus;
  readonly quality: SnapshotQuality;
  readonly input: Input;
}

export interface ViewLoadRequest {
  readonly reason: "mount" | "navigation" | "refresh" | "reconcile";
  readonly knownRevision?: SnapshotRevision;
  readonly bindings?: Readonly<Record<string, JsonValue>>;
}

export interface WidgetLoadRequest {
  readonly viewId: ViewId;
  readonly instanceId: WidgetInstanceId;
  readonly knownRevision?: SnapshotRevision;
  readonly bindings?: Readonly<Record<string, JsonValue>>;
}

interface IntentEnvelope<Payload = unknown> {
  readonly intent_type: string;
  readonly schema_version: number;
  readonly intent_id: string;
  readonly view_id: ViewId;
  /** Present for mutations that require idempotent provider handling. */
  readonly client_mutation_id?: string;
  readonly payload: Payload;
}

export interface ViewIntent<Payload = unknown> extends IntentEnvelope<Payload> {
  readonly instance_id?: never;
}

export interface WidgetIntent<Payload = unknown> extends IntentEnvelope<Payload> {
  readonly instance_id: WidgetInstanceId;
}

export type DashboardIntent = ViewIntent | WidgetIntent;

export interface IntentResult<Value = unknown> {
  readonly intent_id: string;
  readonly client_mutation_id?: string;
  readonly status: "accepted" | "rejected" | "conflict" | "unavailable";
  readonly revision?: SnapshotRevision;
  readonly message?: string;
  readonly value?: Value;
  readonly fieldErrors?: Readonly<Record<string, string>>;
}

export interface AppInvalidation {
  readonly id: string;
  readonly appId: AppId;
  readonly viewIds?: readonly ViewId[];
  readonly revision?: SnapshotRevision;
  readonly reason: string;
  readonly observedAt: string;
}

export interface ReconcileResult {
  readonly changed: boolean;
  readonly revision?: SnapshotRevision;
  readonly snapshot?: ViewSnapshot;
}

export interface WidgetPresentationContext {
  readonly instanceId: WidgetInstanceId;
  readonly viewId: ViewId;
  readonly width: number;
  readonly height: number;
  readonly sizeMode: WidgetSizeMode;
  readonly editing: boolean;
  readonly theme: ResolvedThemeSummary;
  getCanvasTheme(): CanvasThemeSnapshot;
}

export interface WidgetRendererProps<
  Input = unknown,
  OutputIntent extends WidgetIntent = WidgetIntent,
> {
  readonly input: Input;
  emit(intent: OutputIntent): void;
  readonly presentation: WidgetPresentationContext;
}
