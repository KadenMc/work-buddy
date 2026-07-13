import { useCallback, useEffect, useRef, useState } from "react";

import type {
  AppInvalidation,
  DashboardIntent,
  IntentResult,
  JsonValue,
  ReconcileResult,
  SnapshotRevision,
  SnapshotStatus,
  ViewId,
  ViewLoadRequest,
  ViewSnapshot,
} from "../contributions/contracts";
import { useDashboardEvents } from "../events/DashboardEventProvider";
import type { ViewProvider } from "../providers/ViewProvider";

export type ViewSessionStatus = "loading" | SnapshotStatus;

export interface ViewSessionState {
  readonly status: ViewSessionStatus;
  readonly snapshot?: ViewSnapshot;
  readonly error?: Error;
  readonly reconciling: boolean;
  readonly pendingIntentIds: readonly string[];
}

export interface UseViewSessionOptions {
  readonly provider: ViewProvider;
  readonly viewId: ViewId;
  readonly bindings?: Readonly<Record<string, JsonValue>>;
}

export interface ViewSession extends ViewSessionState {
  reload(reason?: Extract<ViewLoadRequest["reason"], "refresh" | "reconcile">): Promise<
    ViewSnapshot | undefined
  >;
  dispatch(intent: DashboardIntent): Promise<IntentResult>;
  reconcile(invalidation: AppInvalidation): Promise<ReconcileResult | undefined>;
}

const initialState = (): ViewSessionState => ({
  status: "loading",
  reconciling: false,
  pendingIntentIds: [],
});

const asError = (value: unknown): Error =>
  value instanceof Error ? value : new Error(String(value));

const sameRevision = (
  left: SnapshotRevision | undefined,
  right: SnapshotRevision | undefined,
): boolean => left !== undefined && right !== undefined && Object.is(left, right);

export const hasNumericRevisionGap = (
  current: SnapshotRevision | undefined,
  incoming: SnapshotRevision | undefined,
): boolean =>
  typeof current === "number" &&
  typeof incoming === "number" &&
  incoming > current + 1;

export function useViewSession({
  provider,
  viewId,
  bindings,
}: UseViewSessionOptions): ViewSession {
  const events = useDashboardEvents();
  const [state, setState] = useState<ViewSessionState>(initialState);
  const snapshotRef = useRef<ViewSnapshot | undefined>(undefined);
  const epochRef = useRef(0);
  const requestRef = useRef(0);
  const handledInvalidationSequence = useRef(events.lastInvalidation?.sequence ?? 0);
  const handledReconcileSequence = useRef(events.reconcileSignal?.sequence ?? 0);

  const isCurrent = useCallback(
    (epoch: number, request: number): boolean =>
      epochRef.current === epoch && requestRef.current === request,
    [],
  );

  const commitSnapshot = useCallback(
    (snapshot: ViewSnapshot, epoch: number, request: number): boolean => {
      if (!isCurrent(epoch, request)) {
        return false;
      }
      if (snapshot.viewId !== viewId) {
        throw new Error(
          `Provider returned snapshot ${snapshot.viewId} for requested view ${viewId}`,
        );
      }
      snapshotRef.current = snapshot;
      setState((current) => ({
        ...current,
        status: snapshot.status,
        snapshot,
        error: undefined,
        reconciling: false,
      }));
      return true;
    },
    [isCurrent, viewId],
  );

  const load = useCallback(
    async (reason: ViewLoadRequest["reason"]): Promise<ViewSnapshot | undefined> => {
      const epoch = epochRef.current;
      const request = ++requestRef.current;
      const currentRevision = snapshotRef.current?.revision;
      setState((current) => ({
        ...current,
        status: current.snapshot === undefined ? "loading" : current.status,
        error: undefined,
        reconciling: reason !== "mount" && reason !== "navigation",
      }));
      try {
        const snapshot = await provider.loadView(viewId, {
          reason,
          ...(currentRevision === undefined ? {} : { knownRevision: currentRevision }),
          ...(bindings === undefined ? {} : { bindings }),
        });
        return commitSnapshot(snapshot, epoch, request) ? snapshot : undefined;
      } catch (error) {
        if (isCurrent(epoch, request)) {
          setState((current) => ({
            ...current,
            status: "error",
            error: asError(error),
            reconciling: false,
          }));
        }
        return undefined;
      }
    },
    [bindings, commitSnapshot, isCurrent, provider, viewId],
  );

  const reconcile = useCallback(
    async (invalidation: AppInvalidation): Promise<ReconcileResult | undefined> => {
      if (invalidation.appId !== provider.appId) {
        return undefined;
      }
      if (
        invalidation.viewIds !== undefined &&
        !invalidation.viewIds.includes(viewId)
      ) {
        return undefined;
      }

      const currentRevision = snapshotRef.current?.revision;
      if (sameRevision(currentRevision, invalidation.revision)) {
        return { changed: false, revision: currentRevision };
      }

      const epoch = epochRef.current;
      const request = ++requestRef.current;
      const providerInvalidation = hasNumericRevisionGap(
        currentRevision,
        invalidation.revision,
      )
        ? { ...invalidation, reason: `revision-gap:${invalidation.reason}` }
        : invalidation;
      setState((current) => ({ ...current, reconciling: true, error: undefined }));

      try {
        const result = await provider.reconcile(providerInvalidation);
        if (!isCurrent(epoch, request)) {
          return undefined;
        }
        if (result.snapshot !== undefined) {
          commitSnapshot(result.snapshot, epoch, request);
          return result;
        }
        if (result.changed) {
          const snapshot = await provider.loadView(viewId, {
            reason: "reconcile",
            ...(currentRevision === undefined ? {} : { knownRevision: currentRevision }),
            ...(bindings === undefined ? {} : { bindings }),
          });
          if (!isCurrent(epoch, request)) {
            return undefined;
          }
          commitSnapshot(snapshot, epoch, request);
          return { ...result, snapshot };
        }
        setState((current) => ({ ...current, reconciling: false }));
        return result;
      } catch (error) {
        if (isCurrent(epoch, request)) {
          setState((current) => ({
            ...current,
            status: "error",
            error: asError(error),
            reconciling: false,
          }));
        }
        return undefined;
      }
    },
    [bindings, commitSnapshot, isCurrent, provider, viewId],
  );

  const dispatch = useCallback(
    async (intent: DashboardIntent): Promise<IntentResult> => {
      if (intent.view_id !== viewId) {
        throw new Error(`Intent ${intent.intent_id} targets ${intent.view_id}, not ${viewId}`);
      }
      const epoch = epochRef.current;
      setState((current) => ({
        ...current,
        pendingIntentIds: current.pendingIntentIds.includes(intent.intent_id)
          ? current.pendingIntentIds
          : [...current.pendingIntentIds, intent.intent_id],
      }));
      try {
        const result = await provider.dispatch(intent);
        if (epochRef.current === epoch && result.status === "accepted") {
          await reconcile({
            id: `intent:${intent.intent_id}`,
            appId: provider.appId,
            viewIds: [viewId],
            ...(result.revision === undefined ? {} : { revision: result.revision }),
            reason: `intent-accepted:${intent.intent_type}`,
            observedAt: new Date().toISOString(),
          });
        }
        return result;
      } catch (error) {
        if (epochRef.current === epoch) {
          setState((current) => ({ ...current, error: asError(error) }));
        }
        throw error;
      } finally {
        if (epochRef.current === epoch) {
          setState((current) => ({
            ...current,
            pendingIntentIds: current.pendingIntentIds.filter(
              (intentId) => intentId !== intent.intent_id,
            ),
          }));
        }
      }
    },
    [provider, reconcile, viewId],
  );

  useEffect(() => {
    epochRef.current += 1;
    requestRef.current += 1;
    snapshotRef.current = undefined;
    setState(initialState());
    void load("mount");
    return () => {
      epochRef.current += 1;
      requestRef.current += 1;
    };
  }, [load]);

  useEffect(() => {
    const sequenced = events.lastInvalidation;
    if (
      sequenced === undefined ||
      sequenced.sequence <= handledInvalidationSequence.current
    ) {
      return;
    }
    handledInvalidationSequence.current = sequenced.sequence;
    const invalidation = sequenced.invalidation;
    void reconcile({
      ...invalidation,
      appId: invalidation.appId ?? provider.appId,
    });
  }, [events.lastInvalidation, provider.appId, reconcile]);

  useEffect(() => {
    const signal = events.reconcileSignal;
    if (signal === undefined || signal.sequence <= handledReconcileSequence.current) {
      return;
    }
    handledReconcileSequence.current = signal.sequence;
    void reconcile({
      id: `dashboard:${signal.reason}:${signal.sequence}`,
      appId: provider.appId,
      viewIds: [viewId],
      reason: `dashboard-${signal.reason}`,
      observedAt: new Date().toISOString(),
    });
  }, [events.reconcileSignal, provider.appId, reconcile, viewId]);

  return {
    ...state,
    reload: (reason = "refresh") => load(reason),
    dispatch,
    reconcile,
  };
}
