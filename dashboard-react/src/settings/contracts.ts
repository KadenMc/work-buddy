import type { SettingsPageId } from "../dashboard/contributions/contracts";

export type BrandedSettingsId<Value extends string, Brand extends string> =
  Value & { readonly __settingsBrand: Brand };

export type SettingId = BrandedSettingsId<string, "setting">;
export type SettingPlacementId = BrandedSettingsId<string, "setting-placement">;

export const asSettingId = (value: string): SettingId => value as SettingId;
export const asSettingPlacementId = (value: string): SettingPlacementId =>
  value as SettingPlacementId;

export type SettingsNavigationGroup =
  | "system"
  | "apps"
  | "connections"
  | "status";

export type SettingsAppCategory = "built-in" | "personal" | "community";

export type SettingsContextKind =
  | "system"
  | "app"
  | "subsystem"
  | "view"
  | "connection"
  | "component"
  | "widget-type"
  | "widget-instance"
  | "status";

export interface SettingsContextRef {
  readonly kind: SettingsContextKind;
  readonly id: string;
  readonly label: string;
}

export type SettingValueScope =
  | "profile"
  | "workspace"
  | "device"
  | "view"
  | "widget-instance";

export type SettingApplyBehavior =
  | "immediate"
  | "reload-view"
  | "restart-component"
  | "restart-dashboard"
  | "next-boundary";

export type SettingsTrustTier =
  | "native"
  | "curated"
  | "community"
  | "developer-local";

export interface SettingsProvenance {
  readonly complementId: string;
  readonly complementVersion: string;
  readonly trustTier: SettingsTrustTier;
  readonly label: string;
}

export type StandardSettingControl =
  | {
      readonly kind: "typography-scale";
      readonly options: readonly string[];
    }
  | {
      readonly kind: "time";
      readonly minuteStep?: number;
    }
  | {
      readonly kind: "switch";
    }
  | {
      readonly kind: "select";
      readonly options: readonly {
        readonly value: string;
        readonly label: string;
        readonly description?: string;
      }[];
    };

export interface SettingDefinition<Value = unknown> {
  readonly schemaVersion: 1;
  readonly settingId: SettingId;
  readonly definitionVersion: number;
  readonly valueVersion: number;
  readonly ownerId: string;
  readonly ownerLabel: string;
  readonly provenance: SettingsProvenance;
  readonly title: string;
  readonly summary: string;
  readonly details?: string;
  readonly valueSchema?: unknown;
  readonly defaultValue: Value;
  readonly allowedScopes: readonly SettingValueScope[];
  readonly defaultScope: SettingValueScope;
  readonly control: StandardSettingControl;
  readonly appliesTo: readonly SettingsContextRef[];
  readonly applyBehavior: SettingApplyBehavior;
  readonly sensitivity: "ordinary" | "private" | "secret-reference";
  readonly visibility: "frontend" | "backend" | "secret";
  readonly searchKeywords?: readonly string[];
}

export interface SettingsSectionDefinition {
  readonly sectionId: string;
  readonly label: string;
  readonly description?: string;
  readonly order: number;
}

export interface SettingsPageContribution {
  readonly schemaVersion: 1;
  readonly pageId: SettingsPageId;
  readonly ownerId: string;
  readonly route: string;
  readonly label: string;
  readonly description: string;
  readonly navigationGroup: SettingsNavigationGroup;
  readonly navigationLabel: string;
  readonly navigationOrder: number;
  /** Required for App pages so the host can present provenance-oriented groups. */
  readonly appCategory?: SettingsAppCategory;
  readonly context: SettingsContextRef;
  readonly sections: readonly SettingsSectionDefinition[];
  readonly fallbackReturnPath?: string;
}

export interface SettingPlacement {
  readonly schemaVersion: 1;
  readonly placementId: SettingPlacementId;
  readonly settingId: SettingId;
  readonly pageId: SettingsPageId;
  readonly sectionId: string;
  readonly order: number;
  readonly contextualSummary?: string;
  readonly preferredForSearch?: boolean;
}

export interface SettingsContribution {
  readonly sourceId: string;
  readonly definitions: readonly SettingDefinition[];
  readonly pages: readonly SettingsPageContribution[];
  readonly placements: readonly SettingPlacement[];
}

export type EffectiveSettingSource =
  | "default"
  | "profile"
  | "workspace"
  | "device"
  | "view"
  | "policy";

export interface EffectiveSettingValue {
  readonly settingId: SettingId;
  readonly scope: {
    readonly kind: SettingValueScope;
    readonly subjectId?: string;
  };
  readonly effectiveValue: unknown;
  readonly configuredValue?: unknown;
  readonly source: EffectiveSettingSource;
  readonly isModified: boolean;
  readonly revision: string;
  readonly pendingValue?: unknown;
  readonly effectiveAt?: string;
  readonly applyStatus?: string;
  readonly impactPreview?: unknown;
  readonly policyTimezone?: string;
  readonly configuredTimezone?: string;
  readonly pendingTimezone?: string;
  readonly diagnostics: readonly SettingsDiagnostic[];
}

export interface SettingsValueSnapshot {
  readonly registryRevision: string;
  readonly timezone?: string;
  readonly configuredTimezone?: string;
  readonly observedAt: string;
  readonly readOnly: boolean;
  readonly diagnostics: readonly SettingsDiagnostic[];
  readonly values: ReadonlyMap<SettingId, EffectiveSettingValue>;
}

export interface SettingsDiagnostic {
  readonly code: string;
  readonly message: string;
  readonly activeTimezone?: string;
  readonly configuredTimezone?: string;
}

export interface ProposedSettingPreview {
  readonly settingId: SettingId;
  readonly scope: {
    readonly kind: SettingValueScope;
    readonly subjectId?: string;
  };
  readonly value: unknown;
  readonly valueRevision: string;
  readonly timezone?: string;
  readonly configuredTimezone?: string;
  readonly effectiveAt?: string;
  readonly applyStatus: string;
  readonly impactPreview?: unknown;
  readonly diagnostics: readonly SettingsDiagnostic[];
}

export interface ProjectedSetting {
  readonly definition: SettingDefinition;
  readonly placement: SettingPlacement;
}

export interface ProjectedSettingsSection {
  readonly definition: SettingsSectionDefinition;
  readonly settings: readonly ProjectedSetting[];
}

export interface ProjectedSettingsPage {
  readonly page: SettingsPageContribution;
  readonly sections: readonly ProjectedSettingsSection[];
}

export interface SettingsSearchResult {
  readonly settingId: SettingId;
  readonly definition: SettingDefinition;
  readonly placement: SettingPlacement;
  readonly page: SettingsPageContribution;
  readonly section: SettingsSectionDefinition;
}
