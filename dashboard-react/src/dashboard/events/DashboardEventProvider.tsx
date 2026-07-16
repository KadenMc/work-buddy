import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  asAppId,
  asViewId,
  type AppId,
  type SnapshotRevision,
  type ViewId,
} from "../contributions/contracts";

export type DashboardConnectionState =
  | "connecting"
  | "live"
  | "reconnecting"
  | "offline";

/** Host-normalized invalidation. Missing appId means every active provider may reconcile. */
export interface DashboardInvalidation {
  readonly id: string;
  readonly appId?: AppId;
  readonly viewIds?: readonly ViewId[];
  readonly revision?: SnapshotRevision;
  readonly reason: string;
  readonly observedAt: string;
}

export interface SequencedDashboardInvalidation {
  readonly sequence: number;
  readonly invalidation: DashboardInvalidation;
}

export interface DashboardReconcileSignal {
  readonly sequence: number;
  readonly reason: "connected" | "reconnected" | "foreground";
}

export interface DashboardEventContextValue {
  readonly connectionState: DashboardConnectionState;
  readonly lastInvalidation?: SequencedDashboardInvalidation;
  readonly reconcileSignal?: DashboardReconcileSignal;
}

interface DashboardEventProviderProps {
  readonly children: ReactNode;
  readonly endpoint?: string;
}

const DashboardEventContext = createContext<DashboardEventContextValue | undefined>(
  undefined,
);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isRevision = (value: unknown): value is SnapshotRevision =>
  typeof value === "string" || (typeof value === "number" && Number.isFinite(value));

const maybeAppId = (value: unknown): AppId | undefined =>
  typeof value === "string" &&
  /^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+$/.test(value)
    ? asAppId(value)
    : undefined;

const maybeViewIds = (value: unknown): readonly ViewId[] | undefined => {
  if (!Array.isArray(value)) {
    return undefined;
  }
  const ids = value
    .filter((candidate): candidate is string => typeof candidate === "string")
    .map(asViewId);
  return ids.length > 0 ? ids : undefined;
};

const observedAt = (value: unknown, fallback: string): string => {
  if (typeof value === "string" && !Number.isNaN(Date.parse(value))) {
    return value;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return new Date(value * 1_000).toISOString();
  }
  return fallback;
};

/**
 * Normalize both the current legacy dashboard envelope and the future CloudEvents
 * projection. Unknown fields are ignored, malformed frames return null, and heartbeat
 * frames never trigger data reconciliation.
 */
export function normalizeDashboardEvent(
  raw: unknown,
  fallbackNow = new Date().toISOString(),
): DashboardInvalidation | null {
  if (!isRecord(raw)) {
    return null;
  }

  if (raw.specversion === "1.0" && typeof raw.type === "string") {
    if (raw.type === "bus.heartbeat") {
      return null;
    }
    const data = isRecord(raw.data) ? raw.data : {};
    const sourceAppId =
      typeof raw.source === "string"
        ? maybeAppId(raw.source.match(/^\/apps\/(.+)$/)?.[1])
        : undefined;
    const revision = data.revision;
    return {
      id:
        typeof raw.id === "string" && raw.id.length > 0
          ? raw.id
          : `cloud:${raw.type}:${fallbackNow}`,
      appId: maybeAppId(data.app_id ?? data.appId) ?? sourceAppId,
      viewIds: maybeViewIds(data.view_ids ?? data.viewIds),
      ...(isRevision(revision) ? { revision } : {}),
      reason: raw.type,
      observedAt: observedAt(raw.time, fallbackNow),
    };
  }

  if (typeof raw.event_type !== "string" || raw.event_type.length === 0) {
    return null;
  }
  if (raw.event_type === "bus.heartbeat") {
    return null;
  }
  const payload = isRecord(raw.payload) ? raw.payload : {};
  const revision = payload.revision;
  return {
    id:
      typeof payload.event_id === "string" && payload.event_id.length > 0
        ? payload.event_id
        : `legacy:${raw.event_type}:${String(raw.ts ?? fallbackNow)}`,
    appId: maybeAppId(payload.app_id ?? payload.appId),
    viewIds: maybeViewIds(payload.view_ids ?? payload.viewIds),
    ...(isRevision(revision) ? { revision } : {}),
    reason: raw.event_type,
    observedAt: observedAt(raw.ts, fallbackNow),
  };
}

export function DashboardEventProvider({
  children,
  endpoint = "/api/events",
}: DashboardEventProviderProps) {
  const [connectionState, setConnectionState] =
    useState<DashboardConnectionState>("connecting");
  const [lastInvalidation, setLastInvalidation] =
    useState<SequencedDashboardInvalidation>();
  const [reconcileSignal, setReconcileSignal] =
    useState<DashboardReconcileSignal>();
  const invalidationSequence = useRef(0);
  const reconcileSequence = useRef(0);

  useEffect(() => {
    setConnectionState("connecting");
    if (typeof EventSource === "undefined") {
      setConnectionState("offline");
      return;
    }

    let source: EventSource;
    try {
      source = new EventSource(endpoint);
    } catch {
      setConnectionState("offline");
      return;
    }

    let hasOpened = false;
    const requestReconcile = (
      reason: DashboardReconcileSignal["reason"],
    ): void => {
      reconcileSequence.current += 1;
      setReconcileSignal({ sequence: reconcileSequence.current, reason });
    };
    const onOpen = (): void => {
      setConnectionState("live");
      requestReconcile(hasOpened ? "reconnected" : "connected");
      hasOpened = true;
    };
    const onError = (): void => setConnectionState("reconnecting");
    const onMessage = (event: MessageEvent<string>): void => {
      try {
        const normalized = normalizeDashboardEvent(JSON.parse(event.data) as unknown);
        if (normalized === null) {
          return;
        }
        invalidationSequence.current += 1;
        setLastInvalidation({
          sequence: invalidationSequence.current,
          invalidation: normalized,
        });
      } catch {
        // Malformed/unknown transport frames are intentionally ignored.
      }
    };
    const onVisibility = (): void => {
      if (document.visibilityState === "visible") {
        requestReconcile("foreground");
      }
    };

    source.addEventListener("open", onOpen);
    source.addEventListener("error", onError);
    source.addEventListener("message", onMessage);
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      source.removeEventListener("open", onOpen);
      source.removeEventListener("error", onError);
      source.removeEventListener("message", onMessage);
      document.removeEventListener("visibilitychange", onVisibility);
      source.close();
    };
  }, [endpoint]);

  const value = useMemo<DashboardEventContextValue>(
    () => ({ connectionState, lastInvalidation, reconcileSignal }),
    [connectionState, lastInvalidation, reconcileSignal],
  );

  return (
    <DashboardEventContext.Provider value={value}>
      {children}
    </DashboardEventContext.Provider>
  );
}

export function useDashboardEvents(): DashboardEventContextValue {
  const context = useContext(DashboardEventContext);
  if (context === undefined) {
    throw new Error("useDashboardEvents must be used within DashboardEventProvider");
  }
  return context;
}
