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
  surfaceCanvas: "#f4f2ed",
  surfaceRaised: "#fffdf9",
  textPrimary: "#26343a",
  textSecondary: "#526168",
  borderDefault: "#d1ccc2",
  focusRing: "#28778b",
  dataSeries: [
    "#2c7181",
    "#28774d",
    "#a76f18",
    "#b84f32",
    "#765b9d",
    "#277c79",
    "#a55227",
    "#667278",
  ],
});

const fallbackDark: CanvasThemeSnapshot = Object.freeze({
  surfaceCanvas: "#0c1519",
  surfaceRaised: "#17252b",
  textPrimary: "#e2eae9",
  textSecondary: "#adbdc0",
  borderDefault: "#30464d",
  focusRing: "#76c4d5",
  dataSeries: [
    "#69b6c8",
    "#68c18c",
    "#dfb255",
    "#e77b5d",
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
