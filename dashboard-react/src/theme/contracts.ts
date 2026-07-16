import type {
  CanvasThemeSnapshot,
  ResolvedThemeSummary,
  ThemePreference,
} from "../dashboard/contributions/themeContract";

export type {
  CanvasThemeSnapshot,
  ResolvedThemeScheme,
  ResolvedThemeSummary,
  ThemeAccessibilityState,
  ThemePreference,
  ThemeSchemePreference,
  ThemeSkinIdentity,
} from "../dashboard/contributions/themeContract";

export interface ResolvedTheme extends ResolvedThemeSummary {
  readonly canvasTokens: CanvasThemeSnapshot;
}

export interface ThemeRuntime {
  readonly theme: ResolvedTheme;
  setPreference(patch: Partial<ThemePreference>): void;
  beginPreview(next: ThemePreference): () => void;
  getCanvasTheme(): CanvasThemeSnapshot;
}
