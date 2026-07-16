/** The public theme surface consumed by standard dashboard contributions. */
export const THEME_CONTRACT_VERSION = 1 as const;

export type ThemeContractVersion = typeof THEME_CONTRACT_VERSION;
export type ThemeSchemePreference = "system" | "light" | "dark";
export type ResolvedThemeScheme = Exclude<ThemeSchemePreference, "system">;
export type WidgetThemeSupport =
  | ResolvedThemeScheme
  | "forced-colors"
  | "reduced-motion";
export type WidgetThemeStyling = "semantic-tokens" | "host-primitives";
export type WidgetThemeConformance = "standard" | "custom";
export type WidgetThemeExceptionKind = "fixed-brand-color" | "media-content";

export interface ThemePreference {
  readonly scheme: ThemeSchemePreference;
  readonly skinId: string;
}

export interface ThemeSkinIdentity {
  readonly id: string;
  readonly version: number;
  readonly publisherAppId: string;
}

export interface ThemeAccessibilityState {
  readonly forcedColors: boolean;
  readonly reducedMotion: boolean;
  readonly reducedTransparency: boolean;
}

export interface WidgetThemeException {
  readonly kind: WidgetThemeExceptionKind;
  readonly reason: string;
}

/**
 * Manifest-level proof obligation for a widget. A standard widget must support the
 * complete v1 matrix and style through host primitives or semantic tokens.
 */
export interface WidgetThemeDeclaration {
  readonly contractVersion: ThemeContractVersion;
  readonly conformance: WidgetThemeConformance;
  readonly supports: readonly WidgetThemeSupport[];
  readonly styling: WidgetThemeStyling;
  readonly exceptions?: readonly WidgetThemeException[];
}

export interface ResolvedThemeSummary {
  readonly contractVersion: ThemeContractVersion;
  readonly preference: ThemePreference;
  readonly resolvedScheme: ResolvedThemeScheme;
  readonly skin: ThemeSkinIdentity;
  readonly accessibility: ThemeAccessibilityState;
}

/** Small serializable bridge for renderers that cannot inherit CSS variables. */
export interface CanvasThemeSnapshot {
  readonly surfaceCanvas: string;
  readonly surfaceRaised: string;
  readonly textPrimary: string;
  readonly textSecondary: string;
  readonly borderDefault: string;
  readonly focusRing: string;
  readonly dataSeries: readonly string[];
}

export const STANDARD_WIDGET_THEME_SUPPORT = [
  "light",
  "dark",
  "forced-colors",
  "reduced-motion",
] as const satisfies readonly WidgetThemeSupport[];
