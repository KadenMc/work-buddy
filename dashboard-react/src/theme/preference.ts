import type { ThemePreference } from "./contracts";
import { DEFAULT_SKIN_ID, isKnownThemeSkin } from "./packs/registry";

export const THEME_PREFERENCE_STORAGE_KEY = "wb.theme.preference.v1";
export const DEFAULT_THEME_PREFERENCE: ThemePreference = Object.freeze({
  scheme: "system",
  skinId: DEFAULT_SKIN_ID,
});

interface StoredThemePreference extends ThemePreference {
  readonly version: 1;
}

const isScheme = (value: unknown): value is ThemePreference["scheme"] =>
  value === "system" || value === "light" || value === "dark";

export function normalizeThemePreference(value: unknown): ThemePreference {
  if (typeof value !== "object" || value === null) {
    return DEFAULT_THEME_PREFERENCE;
  }
  const candidate = value as Partial<StoredThemePreference>;
  if (
    !isScheme(candidate.scheme) ||
    typeof candidate.skinId !== "string" ||
    !isKnownThemeSkin(candidate.skinId)
  ) {
    return DEFAULT_THEME_PREFERENCE;
  }
  return { scheme: candidate.scheme, skinId: candidate.skinId };
}

export function parseStoredThemePreference(
  serialized: string | null,
): ThemePreference {
  if (serialized === null) {
    return DEFAULT_THEME_PREFERENCE;
  }
  try {
    const parsed = JSON.parse(serialized) as unknown;
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      (parsed as { version?: unknown }).version !== 1
    ) {
      return DEFAULT_THEME_PREFERENCE;
    }
    return normalizeThemePreference(parsed);
  } catch {
    return DEFAULT_THEME_PREFERENCE;
  }
}

export function readThemePreference(storage?: Storage): ThemePreference {
  try {
    const target = storage ?? window.localStorage;
    return parseStoredThemePreference(target.getItem(THEME_PREFERENCE_STORAGE_KEY));
  } catch {
    return DEFAULT_THEME_PREFERENCE;
  }
}

export function writeThemePreference(
  preference: ThemePreference,
  storage?: Storage,
): void {
  try {
    const target = storage ?? window.localStorage;
    target.setItem(
      THEME_PREFERENCE_STORAGE_KEY,
      JSON.stringify({ version: 1, ...preference } satisfies StoredThemePreference),
    );
  } catch {
    // Storage may be unavailable or quota-blocked. The in-memory preference remains valid.
  }
}
