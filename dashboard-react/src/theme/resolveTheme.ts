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
  surfaceCanvas: "#f6f8fa",
  surfaceRaised: "#ffffff",
  textPrimary: "#1f2328",
  textSecondary: "#59636e",
  borderDefault: "#d0d7de",
  focusRing: "#0969da",
  dataSeries: [
    "#0969da",
    "#1a7f37",
    "#bf8700",
    "#cf222e",
    "#8250df",
    "#0a7b83",
    "#bc4c00",
    "#57606a",
  ],
});

const fallbackDark: CanvasThemeSnapshot = Object.freeze({
  surfaceCanvas: "#0d1117",
  surfaceRaised: "#161b22",
  textPrimary: "#e6edf3",
  textSecondary: "#9da7b3",
  borderDefault: "#30363d",
  focusRing: "#58a6ff",
  dataSeries: [
    "#58a6ff",
    "#3fb950",
    "#d29922",
    "#f85149",
    "#bc8cff",
    "#39c5cf",
    "#db6d28",
    "#8b949e",
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
