import { THEME_CONTRACT_VERSION } from "../dashboard/contributions/themeContract";
import type {
  CanvasThemeSnapshot,
  ResolvedThemeScheme,
  ResolvedThemeSummary,
  ThemePreference,
} from "./contracts";
import { getThemeSkin } from "./packs/registry";

export interface ThemeEnvironment {
  readonly prefersDark: boolean;
  readonly forcedColors: boolean;
  readonly reducedMotion: boolean;
  readonly reducedTransparency: boolean;
}

export const resolveThemeScheme = (
  preference: ThemePreference,
  prefersDark: boolean,
): ResolvedThemeScheme =>
  preference.scheme === "system"
    ? prefersDark
      ? "dark"
      : "light"
    : preference.scheme;

export function resolveThemeSummary(
  preference: ThemePreference,
  environment: ThemeEnvironment,
): ResolvedThemeSummary {
  const skin = getThemeSkin(preference.skinId);
  return {
    contractVersion: THEME_CONTRACT_VERSION,
    preference,
    resolvedScheme: resolveThemeScheme(preference, environment.prefersDark),
    skin: skin.identity,
    accessibility: {
      forcedColors: environment.forcedColors,
      reducedMotion: environment.reducedMotion,
      reducedTransparency: environment.reducedTransparency,
    },
  };
}

const fallbackLight: CanvasThemeSnapshot = Object.freeze({
  surfaceCanvas: "#f6f1e9",
  surfaceRaised: "#fffaf4",
  textPrimary: "#3b2b25",
  textSecondary: "#69564d",
  borderDefault: "#d8c7b8",
  focusRing: "#bd4c24",
  dataSeries: [
    "#b64d28",
    "#28774d",
    "#276f77",
    "#a76f18",
    "#765b9d",
    "#277c79",
    "#a55227",
    "#667278",
  ],
});

const fallbackDark: CanvasThemeSnapshot = Object.freeze({
  surfaceCanvas: "#15110f",
  surfaceRaised: "#251e1a",
  textPrimary: "#eee2d8",
  textSecondary: "#c4b2a6",
  borderDefault: "#4e3d34",
  focusRing: "#ff9a63",
  dataSeries: [
    "#ff9a63",
    "#68c18c",
    "#73c3c9",
    "#dfb255",
    "#b39add",
    "#65c2ba",
    "#dc8b54",
    "#93a4a8",
  ],
});

export const fallbackCanvasTheme = (
  scheme: ResolvedThemeScheme,
): CanvasThemeSnapshot => (scheme === "dark" ? fallbackDark : fallbackLight);

export function readCanvasTheme(
  scheme: ResolvedThemeScheme,
  element?: Element,
): CanvasThemeSnapshot {
  if (typeof window === "undefined" || typeof getComputedStyle === "undefined") {
    return fallbackCanvasTheme(scheme);
  }
  const target = element ?? document.documentElement;
  const styles = getComputedStyle(target);
  const fallback = fallbackCanvasTheme(scheme);
  const read = (name: string, defaultValue: string): string =>
    styles.getPropertyValue(name).trim() || defaultValue;

  return {
    surfaceCanvas: read("--wb-color-surface-canvas", fallback.surfaceCanvas),
    surfaceRaised: read("--wb-color-surface-raised", fallback.surfaceRaised),
    textPrimary: read("--wb-color-text-primary", fallback.textPrimary),
    textSecondary: read("--wb-color-text-secondary", fallback.textSecondary),
    borderDefault: read("--wb-color-border-default", fallback.borderDefault),
    focusRing: read("--wb-color-focus-ring", fallback.focusRing),
    dataSeries: Array.from({ length: 8 }, (_, index) =>
      read(`--wb-color-data-${index + 1}`, fallback.dataSeries[index]!),
    ),
  };
}
