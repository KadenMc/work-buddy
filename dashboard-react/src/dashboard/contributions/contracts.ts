import type {
  CanvasThemeSnapshot,
  ResolvedThemeSummary,
  WidgetThemeDeclaration,
} from "./themeContract";
import type { HelpContent } from "../help/contracts";

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
export type ViewModuleId = DashboardId<"view-module">;
/** Stable identity resolved by the host-owned Settings registry, never a route. */
export type SettingsPageId = DashboardId<"settings-page">;

export const asAppId = (value: string): AppId => value as AppId;
export const asViewId = (value: string): ViewId => value as ViewId;
export const asWidgetTypeId = (value: string): WidgetTypeId => value as WidgetTypeId;
export const asWidgetSlotId = (value: string): WidgetSlotId => value as WidgetSlotId;
export const asWidgetInstanceId = (value: string): WidgetInstanceId =>
  value as WidgetInstanceId;
export const asWidgetRoleId = (value: string): WidgetRoleId => value as WidgetRoleId;
export const asWidgetModuleId = (value: string): WidgetModuleId => value as WidgetModuleId;
export const asViewModuleId = (value: string): ViewModuleId => value as ViewModuleId;
export const asSettingsPageId = (value: string): SettingsPageId => value as SettingsPageId;

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

export type WidgetDraftPersistence = "device" | "session" | "none";
export type WidgetDraftSensitivity = "ordinary" | "private" | "secret";
export type WidgetDraftClearPolicy = "confirm" | "undoable" | "widget-managed";

export type WidgetDraftScope =
  | { readonly kind: "view" }
  | {
      readonly kind: "input-field";
      /** Path within the validated widget input, for example `["dayId"]`. */
      readonly path: readonly string[];
    };

/** Host-owned recoverable working state, separate from settings and provider data. */
export interface WidgetDraftDeclaration {
  readonly draftName: string;
  readonly schema: JsonSchemaReference;
  readonly persistence: WidgetDraftPersistence;
  readonly sensitivity: WidgetDraftSensitivity;
  readonly retentionDays?: number;
  readonly maxBytes: number;
  readonly clearPolicy: WidgetDraftClearPolicy;
  readonly scope: WidgetDraftScope;
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

export type WidgetIntentEffect = "read" | "mutation" | "navigation" | "external";
export type WidgetIntentPreviewPolicy = "simulate" | "block";

export interface WidgetIntentEffectDeclaration {
  readonly schema: JsonSchemaReference;
  readonly effect: WidgetIntentEffect;
  readonly preview: WidgetIntentPreviewPolicy;
}

/** A reusable library type. It never identifies a placement in a view. */
export interface WidgetDefinition {
  readonly typeId: WidgetTypeId;
  readonly definitionVersion: number;
  readonly publisherAppId: AppId;
  readonly displayName: string;
  readonly description: string;
  /** Reusable type-level help; a view placement may override this with its specific job. */
  readonly help?: HelpContent;
  readonly libraryPath: readonly string[];
  readonly providesRoles: readonly WidgetRoleId[];
  readonly settingsSchema: JsonSchemaReference;
  readonly inputSchema: JsonSchemaReference;
  readonly outputIntentSchemas: readonly JsonSchemaReference[];
  /** Semantic effect policy for every outward intent; local UI actions do not belong here. */
  readonly outputIntentEffects?: readonly WidgetIntentEffectDeclaration[];
  readonly drafts?: readonly WidgetDraftDeclaration[];
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
  /** The job this stable placement performs in this particular view. */
  readonly help: HelpContent;
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
  /**
   * A discoverability reference only. Dashboard Core resolves the page ID and owns
   * navigation; an App may provide the user-facing label but never a route or store.
   */
  readonly settings?: {
    readonly pageId: SettingsPageId;
    readonly label: string;
  };
  readonly grid: DashboardGridDefinition;
  readonly defaultSlots: readonly DefaultWidgetSlot[];
  readonly readingOrder: readonly WidgetSlotId[];
  readonly mobileOrder: readonly WidgetSlotId[];
}

/** Pure contribution data; executable widget/view modules are registered separately. */
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
  readonly interactionMode: "operate" | "arrange" | "preview";
  /** @deprecated Prefer interactionMode; retained during renderer migration. */
  readonly editing: boolean;
  readonly theme: ResolvedThemeSummary;
  getCanvasTheme(): CanvasThemeSnapshot;
}

export interface WidgetRendererProps<
  Input = unknown,
  OutputIntent extends WidgetIntent = WidgetIntent,
> {
  readonly input: Input;
  /**
   * Dispatch an intent through the owning App provider and receive its authoritative
   * result. Renderers may ignore the Promise for fire-and-reconcile interactions, but
   * optimistic surfaces such as calendars must await it before committing a mutation.
   */
  emit(intent: OutputIntent): Promise<IntentResult>;
  readonly presentation: WidgetPresentationContext;
}
