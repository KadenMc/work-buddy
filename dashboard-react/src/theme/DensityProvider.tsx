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

export type DashboardDensity = "compact" | "comfortable" | "spacious";

export const DEFAULT_DASHBOARD_DENSITY: DashboardDensity = "comfortable";
export const DASHBOARD_DENSITY_STORAGE_KEY = "wb.appearance.density.v1";

const isDashboardDensity = (value: unknown): value is DashboardDensity =>
  value === "compact" || value === "comfortable" || value === "spacious";

export function readDashboardDensity(storage?: Storage): DashboardDensity {
  try {
    const target = storage ?? window.localStorage;
    const stored = target.getItem(DASHBOARD_DENSITY_STORAGE_KEY);
    return isDashboardDensity(stored) ? stored : DEFAULT_DASHBOARD_DENSITY;
  } catch {
    return DEFAULT_DASHBOARD_DENSITY;
  }
}

export function writeDashboardDensity(
  density: DashboardDensity,
  storage?: Storage,
): void {
  try {
    (storage ?? window.localStorage).setItem(
      DASHBOARD_DENSITY_STORAGE_KEY,
      density,
    );
  } catch {
    // The live preference still works when storage is unavailable.
  }
}

interface DensityRuntime {
  readonly density: DashboardDensity;
  setDensity(density: DashboardDensity): void;
}

const DensityContext = createContext<DensityRuntime | null>(null);

export function DensityProvider({
  children,
  initialDensity,
}: {
  readonly children: ReactNode;
  readonly initialDensity?: DashboardDensity;
}) {
  const [density, setDensityState] = useState<DashboardDensity>(
    initialDensity ??
      (typeof window === "undefined"
        ? DEFAULT_DASHBOARD_DENSITY
        : readDashboardDensity()),
  );

  useLayoutEffect(() => {
    document.documentElement.dataset.wbDensity = density;
  }, [density]);

  useEffect(() => {
    const synchronize = (event: StorageEvent) => {
      if (
        event.key === DASHBOARD_DENSITY_STORAGE_KEY &&
        isDashboardDensity(event.newValue)
      ) {
        setDensityState(event.newValue);
      }
    };
    window.addEventListener("storage", synchronize);
    return () => window.removeEventListener("storage", synchronize);
  }, []);

  const setDensity = useCallback((next: DashboardDensity) => {
    setDensityState(next);
    writeDashboardDensity(next);
  }, []);

  const runtime = useMemo(() => ({ density, setDensity }), [density, setDensity]);
  return (
    <DensityContext.Provider value={runtime}>{children}</DensityContext.Provider>
  );
}

export function useDensity(): DensityRuntime {
  const runtime = useContext(DensityContext);
  if (runtime === null) {
    throw new Error("useDensity must be used within DensityProvider");
  }
  return runtime;
}
