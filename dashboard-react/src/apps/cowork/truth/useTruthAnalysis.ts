import { useCallback, useEffect, useRef, useState } from "react";

import type {
  TruthAnalysisCapabilities,
  TruthAnalysisProvider,
  TruthAnalysisRun,
} from "./contracts";

export type TruthAnalysisLoadStatus = "loading" | "ready" | "error";

const messageOf = (cause: unknown): string =>
  cause instanceof Error && cause.message.trim().length > 0
    ? cause.message
    : "Truth analysis could not be loaded.";

const isActive = (run: TruthAnalysisRun | null): boolean =>
  run?.status === "queued" || run?.status === "running";

export interface TruthAnalysisDataResult {
  readonly run: TruthAnalysisRun | null;
  readonly status: TruthAnalysisLoadStatus;
  readonly error: string | null;
  readonly capabilities: TruthAnalysisCapabilities | null;
  readonly capabilitiesStatus: TruthAnalysisLoadStatus;
  readonly capabilitiesError: string | null;
  reload(): void;
  adopt(run: TruthAnalysisRun): void;
}

/**
 * Durable run binding. SSE/provider invalidations are hints; a small poll runs
 * only while the current job is active so a missed event cannot strand it.
 */
export const useTruthAnalysis = (
  provider: TruthAnalysisProvider | null,
  pollIntervalMs = 2_000,
): TruthAnalysisDataResult => {
  const [run, setRun] = useState<TruthAnalysisRun | null>(null);
  const [status, setStatus] = useState<TruthAnalysisLoadStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [capabilities, setCapabilities] =
    useState<TruthAnalysisCapabilities | null>(null);
  const [capabilitiesStatus, setCapabilitiesStatus] =
    useState<TruthAnalysisLoadStatus>("loading");
  const [capabilitiesError, setCapabilitiesError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [pollRevision, setPollRevision] = useState(0);
  const loadSequence = useRef(0);
  const pollGeneration = useRef(0);
  const boundProvider = useRef(provider);
  const capabilitiesSequence = useRef(0);
  const boundCapabilitiesProvider = useRef(provider);

  const adopt = useCallback((next: TruthAnalysisRun): void => {
    loadSequence.current += 1;
    pollGeneration.current += 1;
    setRun(next);
    setStatus("ready");
    setError(null);
    setPollRevision((value) => value + 1);
  }, []);

  useEffect(() => {
    const providerChanged = boundProvider.current !== provider;
    boundProvider.current = provider;
    if (providerChanged) {
      loadSequence.current += 1;
      pollGeneration.current += 1;
      setRun(null);
      setError(null);
    }
    if (provider === null) {
      loadSequence.current += 1;
      pollGeneration.current += 1;
      setRun(null);
      setStatus("ready");
      setError(null);
      return undefined;
    }
    let live = true;
    const loadCurrent = (initial: boolean): void => {
      const request = ++loadSequence.current;
      if (initial) setStatus("loading");
      void provider.loadCurrent().then(
        (next) => {
          if (!live || request !== loadSequence.current) return;
          // A current-run projection supersedes any poll already in flight.
          pollGeneration.current += 1;
          setRun(next);
          setStatus("ready");
          setError(null);
          setPollRevision((value) => value + 1);
        },
        (cause: unknown) => {
          if (!live || request !== loadSequence.current) return;
          if (initial) setStatus("error");
          setError(messageOf(cause));
        },
      );
    };
    loadCurrent(true);
    const unsubscribe = provider.subscribe(() => loadCurrent(false));
    return () => {
      live = false;
      unsubscribe();
    };
  }, [provider, reloadToken]);

  useEffect(() => {
    const providerChanged = boundCapabilitiesProvider.current !== provider;
    boundCapabilitiesProvider.current = provider;
    const request = ++capabilitiesSequence.current;
    if (providerChanged) {
      setCapabilities(null);
      setCapabilitiesError(null);
    }
    if (provider === null) {
      setCapabilities(null);
      setCapabilitiesStatus("ready");
      setCapabilitiesError(null);
      return undefined;
    }
    let live = true;
    setCapabilitiesStatus("loading");
    void provider.loadCapabilities().then(
      (next) => {
        if (!live || request !== capabilitiesSequence.current) return;
        setCapabilities(next);
        setCapabilitiesStatus("ready");
        setCapabilitiesError(null);
      },
      (cause: unknown) => {
        if (!live || request !== capabilitiesSequence.current) return;
        setCapabilities(null);
        setCapabilitiesStatus("error");
        setCapabilitiesError(messageOf(cause));
      },
    );
    return () => {
      live = false;
    };
  }, [provider, reloadToken]);

  useEffect(() => {
    const currentRun = run;
    if (
      provider === null ||
      !isActive(currentRun) ||
      currentRun === null ||
      pollIntervalMs <= 0
    ) {
      return undefined;
    }
    let live = true;
    const analysisRunId = currentRun.analysisRunId;
    const generation = pollGeneration.current;
    let timer: number | undefined;
    const schedule = (): void => {
      timer = window.setTimeout(() => {
        void poll();
      }, pollIntervalMs);
    };
    const poll = async (): Promise<void> => {
      try {
        const next = await provider.loadRun(analysisRunId);
        if (!live || generation !== pollGeneration.current) return;
        setRun(next);
        setStatus("ready");
        setError(null);
        if (isActive(next)) schedule();
      } catch (cause: unknown) {
        if (!live || generation !== pollGeneration.current) return;
        // Keep the durable visible run; polling failures are recoverable.
        setError(messageOf(cause));
        schedule();
      }
    };
    schedule();
    return () => {
      live = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [pollIntervalMs, pollRevision, provider, run]);

  const reload = useCallback(() => {
    setReloadToken((value) => value + 1);
  }, []);

  return {
    run,
    status,
    error,
    capabilities,
    capabilitiesStatus,
    capabilitiesError,
    reload,
    adopt,
  };
};
