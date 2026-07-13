import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type {
  CanvasThemeSnapshot,
  ThemePreference,
  ThemeRuntime,
} from "./contracts";
import {
  DEFAULT_THEME_PREFERENCE,
  normalizeThemePreference,
  parseStoredThemePreference,
  readThemePreference,
  THEME_PREFERENCE_STORAGE_KEY,
  writeThemePreference,
} from "./preference";
import {
  fallbackCanvasTheme,
  readCanvasTheme,
  resolveThemeSummary,
} from "./resolveTheme";

const ThemeContext = createContext<ThemeRuntime | null>(null);

interface ThemePreviewFrame {
  readonly token: symbol;
  readonly value: ThemePreference;
  readonly previous: ThemePreviewFrame | null;
}

function useMediaQuery(query: string, enabled = true): boolean {
  const read = useCallback(
    () =>
      typeof window !== "undefined" && typeof window.matchMedia === "function"
        ? window.matchMedia(query).matches
        : false,
    [query],
  );
  const [matches, setMatches] = useState(read);

  useEffect(() => {
    if (!enabled || typeof window.matchMedia !== "function") {
      return;
    }
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [enabled, query]);

  return matches;
}

const canvasThemesEqual = (
  left: CanvasThemeSnapshot,
  right: CanvasThemeSnapshot,
): boolean =>
  left.surfaceCanvas === right.surfaceCanvas &&
  left.surfaceRaised === right.surfaceRaised &&
  left.textPrimary === right.textPrimary &&
  left.textSecondary === right.textSecondary &&
  left.borderDefault === right.borderDefault &&
  left.focusRing === right.focusRing &&
  left.dataSeries.every((value, index) => value === right.dataSeries[index]);

export interface ThemeProviderProps {
  readonly children: ReactNode;
  /** Test/preview injection. Production reads the validated local bootstrap mirror. */
  readonly initialPreference?: ThemePreference;
}

export function ThemeProvider({
  children,
  initialPreference,
}: ThemeProviderProps) {
  const initial = normalizeThemePreference(
    initialPreference ??
      (typeof window === "undefined"
        ? DEFAULT_THEME_PREFERENCE
        : readThemePreference()),
  );
  const [preference, setPreferenceState] = useState<ThemePreference>(initial);
  const preferenceRef = useRef(preference);
  const [preview, setPreview] = useState<ThemePreference | null>(null);
  const previewFrameRef = useRef<ThemePreviewFrame | null>(null);
  const effectivePreference = preview ?? preference;
  const usesSystemScheme = effectivePreference.scheme === "system";
  const prefersDark = useMediaQuery(
    "(prefers-color-scheme: dark)",
    usesSystemScheme,
  );
  const forcedColors = useMediaQuery("(forced-colors: active)");
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  const reducedTransparency = useMediaQuery(
    "(prefers-reduced-transparency: reduce)",
  );

  const summary = useMemo(
    () =>
      resolveThemeSummary(effectivePreference, {
        prefersDark,
        forcedColors,
        reducedMotion,
        reducedTransparency,
      }),
    [
      effectivePreference,
      forcedColors,
      prefersDark,
      reducedMotion,
      reducedTransparency,
    ],
  );
  const [canvasTokens, setCanvasTokens] = useState<CanvasThemeSnapshot>(() =>
    fallbackCanvasTheme(summary.resolvedScheme),
  );

  useLayoutEffect(() => {
    const root = document.documentElement;
    root.dataset.wbScheme = summary.resolvedScheme;
    root.dataset.wbSkin = summary.skin.id;
    root.style.colorScheme = summary.resolvedScheme;

    const nextCanvasTokens = readCanvasTheme(summary.resolvedScheme, root);
    setCanvasTokens((current) =>
      canvasThemesEqual(current, nextCanvasTokens) ? current : nextCanvasTokens,
    );
    const themeColor = nextCanvasTokens.surfaceCanvas;
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", themeColor);
  }, [summary.resolvedScheme, summary.skin.id]);

  useEffect(() => {
    const synchronize = (event: StorageEvent) => {
      if (event.key !== THEME_PREFERENCE_STORAGE_KEY) {
        return;
      }
      const next = parseStoredThemePreference(event.newValue);
      preferenceRef.current = next;
      setPreferenceState(next);
    };
    window.addEventListener("storage", synchronize);
    return () => window.removeEventListener("storage", synchronize);
  }, []);

  const setPreference = useCallback((patch: Partial<ThemePreference>) => {
    const next = normalizeThemePreference({
      ...preferenceRef.current,
      ...patch,
    });
    preferenceRef.current = next;
    setPreferenceState(next);
    writeThemePreference(next);
  }, []);

  const beginPreview = useCallback((nextValue: ThemePreference) => {
    const next = normalizeThemePreference(nextValue);
    const token = Symbol("theme-preview");
    const frame: ThemePreviewFrame = {
      token,
      value: next,
      previous: previewFrameRef.current,
    };
    previewFrameRef.current = frame;
    setPreview(next);

    return () => {
      if (previewFrameRef.current?.token !== token) {
        return;
      }
      previewFrameRef.current = frame.previous;
      setPreview(frame.previous?.value ?? null);
    };
  }, []);

  const runtime = useMemo<ThemeRuntime>(
    () => ({
      theme: { ...summary, canvasTokens },
      setPreference,
      beginPreview,
      getCanvasTheme: () => canvasTokens,
    }),
    [beginPreview, canvasTokens, setPreference, summary],
  );

  return <ThemeContext.Provider value={runtime}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeRuntime {
  const runtime = useContext(ThemeContext);
  if (runtime === null) {
    throw new Error("useTheme must be used within ThemeProvider");
  }
  return runtime;
}
