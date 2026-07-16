import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export const DASHBOARD_CONTEXT_ENDPOINT = "/api/dashboard/context" as const;

export interface DashboardTemporalContext {
  readonly schemaVersion: 1;
  readonly revision: string;
  /** Validated IANA zone from Work Buddy's configured USER_TZ. */
  readonly timezone: string;
  /** Server-observed instant used to anchor the shared dashboard clock. */
  readonly now: string;
}

export type DashboardTemporalState =
  | { readonly status: "loading"; readonly context?: undefined }
  | { readonly status: "ready"; readonly context: DashboardTemporalContext }
  | { readonly status: "unavailable"; readonly context?: undefined };

interface DashboardTemporalContextProviderProps {
  readonly children: ReactNode;
  readonly endpoint?: string;
  readonly fetchImpl?: typeof fetch;
  readonly initialContext?: DashboardTemporalContext;
}

const DashboardTemporalContextValue = createContext<DashboardTemporalState>({
  status: "unavailable",
});

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSupportedTimezone(value: unknown): value is string {
  if (typeof value !== "string" || value.length === 0) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value }).format(0);
    return true;
  } catch {
    return false;
  }
}

export function parseDashboardTemporalContext(
  value: unknown,
): DashboardTemporalContext {
  if (
    !isRecord(value) ||
    value.schema_version !== 1 ||
    typeof value.revision !== "string" ||
    value.revision.length === 0 ||
    !isSupportedTimezone(value.timezone) ||
    typeof value.now !== "string" ||
    !Number.isFinite(Date.parse(value.now))
  ) {
    throw new Error("Dashboard context has invalid temporal metadata");
  }
  return {
    schemaVersion: 1,
    revision: value.revision,
    timezone: value.timezone,
    now: value.now,
  };
}

export function DashboardTemporalContextProvider({
  children,
  endpoint = DASHBOARD_CONTEXT_ENDPOINT,
  fetchImpl = fetch,
  initialContext,
}: DashboardTemporalContextProviderProps) {
  const [state, setState] = useState<DashboardTemporalState>(() =>
    initialContext === undefined
      ? { status: "loading" }
      : { status: "ready", context: initialContext },
  );

  useEffect(() => {
    if (initialContext !== undefined) return;
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetchImpl(endpoint, {
          method: "GET",
          headers: { Accept: "application/json" },
          credentials: "same-origin",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`Dashboard context returned ${response.status}`);
        const context = parseDashboardTemporalContext(await response.json());
        if (!controller.signal.aborted) setState({ status: "ready", context });
      } catch {
        if (!controller.signal.aborted) setState({ status: "unavailable" });
      }
    })();
    return () => controller.abort();
  }, [endpoint, fetchImpl, initialContext]);

  const value = useMemo(() => state, [state]);
  return (
    <DashboardTemporalContextValue.Provider value={value}>
      {children}
    </DashboardTemporalContextValue.Provider>
  );
}

export function useDashboardTemporalContext(): DashboardTemporalState {
  return useContext(DashboardTemporalContextValue);
}
