import { useCallback, useEffect, useRef, useState } from "react";

import type {
  TruthClaimDetail,
  TruthClaimsSnapshot,
  TruthQuery,
  TruthRailProvider,
} from "./contracts";

export type TruthLoadStatus = "loading" | "ready" | "error";

const errorMessage = (cause: unknown, fallback: string): string =>
  cause instanceof Error && cause.message.trim().length > 0
    ? cause.message
    : fallback;

export interface TruthDataResult {
  readonly data: TruthClaimsSnapshot | null;
  readonly status: TruthLoadStatus;
  readonly error: string | null;
  reload(): void;
}

/**
 * Race-safe load/subscription binding. Silent invalidations retain the visible
 * snapshot, while initial load and explicit retry expose honest error states.
 */
export const useTruthData = (
  provider: TruthRailProvider,
  query: TruthQuery,
): TruthDataResult => {
  const [data, setData] = useState<TruthClaimsSnapshot | null>(null);
  const [status, setStatus] = useState<TruthLoadStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const sequence = useRef(0);
  const hasData = useRef(false);
  const queryIdentity = `${query.scope}\u0000${query.filter}`;
  const previousQueryIdentity = useRef<string | null>(null);

  useEffect(() => {
    let live = true;
    const load = (showLoading: boolean) => {
      const request = ++sequence.current;
      if (showLoading) {
        setStatus("loading");
        setError(null);
      } else {
        setError(null);
      }
      void provider.load(query).then(
        (next) => {
          if (!live || request !== sequence.current) return;
          hasData.current = true;
          setData(next);
          setStatus("ready");
          setError(null);
        },
        (cause: unknown) => {
          if (!live || request !== sequence.current) return;
          if (showLoading) {
            setStatus("error");
            setError(errorMessage(cause, "Truth could not be loaded."));
          } else {
            // Keep the last authoritative snapshot in place, but do not imply
            // that a failed invalidation refresh succeeded.
            setError(
              errorMessage(
                cause,
                "Truth could not be refreshed. The visible information may be out of date.",
              ),
            );
          }
        },
      );
    };

    const queryChanged = previousQueryIdentity.current !== queryIdentity;
    previousQueryIdentity.current = queryIdentity;
    load(queryChanged || !hasData.current);
    const unsubscribe = provider.subscribe(() => load(false));
    return () => {
      live = false;
      unsubscribe();
    };
  }, [provider, query.filter, query.scope, queryIdentity, reloadToken]);

  const reload = useCallback(() => setReloadToken((value) => value + 1), []);
  return { data, status, error, reload };
};

export interface TruthClaimDetailResult {
  readonly detail: TruthClaimDetail | null;
  readonly status: TruthLoadStatus;
  readonly error: string | null;
  reload(): void;
}

export const useTruthClaimDetail = (
  provider: TruthRailProvider,
  claimId: string | null,
): TruthClaimDetailResult => {
  const [detail, setDetail] = useState<TruthClaimDetail | null>(null);
  const [status, setStatus] = useState<TruthLoadStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const sequence = useRef(0);
  const loadedClaimId = useRef<string | null>(null);

  useEffect(() => {
    if (claimId === null) {
      sequence.current += 1;
      loadedClaimId.current = null;
      setDetail(null);
      setStatus("ready");
      setError(null);
      return undefined;
    }
    let live = true;
    const load = (): void => {
      const request = ++sequence.current;
      const retainingCurrentClaim = loadedClaimId.current === claimId;
      if (!retainingCurrentClaim) {
        setDetail(null);
        setStatus("loading");
      }
      setError(null);
      void provider.loadClaim(claimId).then(
        (next) => {
          if (!live || request !== sequence.current) return;
          loadedClaimId.current = claimId;
          setDetail(next);
          setStatus("ready");
          setError(null);
        },
        (cause: unknown) => {
          if (!live || request !== sequence.current) return;
          setStatus(retainingCurrentClaim ? "ready" : "error");
          setError(
            errorMessage(
              cause,
              retainingCurrentClaim
                ? "This claim could not be refreshed. The visible details may be out of date."
                : "This claim could not be loaded.",
            ),
          );
        },
      );
    };
    load();
    const unsubscribe = provider.subscribe(load);
    return () => {
      live = false;
      unsubscribe();
    };
  }, [claimId, provider, reloadToken]);

  const reload = useCallback(() => setReloadToken((value) => value + 1), []);
  return {
    detail,
    status,
    error,
    reload,
  };
};
