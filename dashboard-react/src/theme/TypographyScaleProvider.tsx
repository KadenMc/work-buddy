import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";

export type TypographyScale = "standard" | "large" | "extra-large" | "maximum";

export interface TypographyScaleOption {
  readonly value: TypographyScale;
  readonly label: string;
  readonly percentage: number;
  readonly description: string;
}

export const TYPOGRAPHY_SCALE_OPTIONS: readonly TypographyScaleOption[] = [
  {
    value: "standard",
    label: "Standard",
    percentage: 100,
    description: "The default Work Buddy type scale.",
  },
  {
    value: "large",
    label: "Large",
    percentage: 112.5,
    description: "Larger text while retaining a roomy dashboard layout.",
  },
  {
    value: "extra-large",
    label: "Extra large",
    percentage: 125,
    description: "A stronger increase for easier reading.",
  },
  {
    value: "maximum",
    label: "Maximum",
    percentage: 137.5,
    description: "The largest supported dashboard text tier.",
  },
] as const;

export const DEFAULT_TYPOGRAPHY_SCALE: TypographyScale = "standard";
export const TYPOGRAPHY_SCALE_STORAGE_KEY = "wb.accessibility.type-scale.v1";

export const isTypographyScale = (value: unknown): value is TypographyScale =>
  TYPOGRAPHY_SCALE_OPTIONS.some((option) => option.value === value);

export function readTypographyScale(storage?: Storage): TypographyScale {
  try {
    const target = storage ?? window.localStorage;
    const stored = target.getItem(TYPOGRAPHY_SCALE_STORAGE_KEY);
    return isTypographyScale(stored) ? stored : DEFAULT_TYPOGRAPHY_SCALE;
  } catch {
    return DEFAULT_TYPOGRAPHY_SCALE;
  }
}

export function writeTypographyScale(
  scale: TypographyScale,
  storage?: Storage,
): void {
  try {
    (storage ?? window.localStorage).setItem(TYPOGRAPHY_SCALE_STORAGE_KEY, scale);
  } catch {
    // The live preference still applies when storage is unavailable.
  }
}

interface TypographyScaleRuntime {
  readonly scale: TypographyScale;
  readonly option: TypographyScaleOption;
  setScale(scale: TypographyScale): void;
  resetScale(): void;
}

const TypographyScaleContext = createContext<TypographyScaleRuntime | null>(null);

export function TypographyScaleProvider({
  children,
  initialScale,
}: {
  readonly children: ReactNode;
  readonly initialScale?: TypographyScale;
}) {
  const [scale, setScaleState] = useState<TypographyScale>(
    initialScale ??
      (typeof window === "undefined"
        ? DEFAULT_TYPOGRAPHY_SCALE
        : readTypographyScale()),
  );

  useLayoutEffect(() => {
    document.documentElement.dataset.wbTypeScale = scale;
  }, [scale]);

  useEffect(() => {
    const synchronize = (event: StorageEvent) => {
      if (
        event.key === TYPOGRAPHY_SCALE_STORAGE_KEY &&
        isTypographyScale(event.newValue)
      ) {
        setScaleState(event.newValue);
      }
    };
    window.addEventListener("storage", synchronize);
    return () => window.removeEventListener("storage", synchronize);
  }, []);

  const setScale = useCallback((next: TypographyScale) => {
    setScaleState(next);
    writeTypographyScale(next);
  }, []);

  const resetScale = useCallback(() => {
    setScale(DEFAULT_TYPOGRAPHY_SCALE);
  }, [setScale]);

  const option =
    TYPOGRAPHY_SCALE_OPTIONS.find((candidate) => candidate.value === scale) ??
    TYPOGRAPHY_SCALE_OPTIONS[0];
  const runtime = useMemo<TypographyScaleRuntime>(
    () => ({ scale, option, setScale, resetScale }),
    [option, resetScale, scale, setScale],
  );

  return (
    <TypographyScaleContext.Provider value={runtime}>
      {children}
    </TypographyScaleContext.Provider>
  );
}

export function useTypographyScale(): TypographyScaleRuntime {
  const runtime = useContext(TypographyScaleContext);
  if (runtime === null) {
    throw new Error(
      "useTypographyScale must be used within TypographyScaleProvider",
    );
  }
  return runtime;
}
