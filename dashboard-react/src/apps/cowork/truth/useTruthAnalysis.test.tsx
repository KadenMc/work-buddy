import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  TruthAnalysisCapabilities,
  TruthAnalysisProvider,
  TruthAnalysisRun,
} from "./contracts";
import { useTruthAnalysis } from "./useTruthAnalysis";

const run = (
  analysisRunId: string,
  status: TruthAnalysisRun["status"],
): TruthAnalysisRun => ({
  schema: "wb.cowork.truth-analysis-run/v1",
  analysisRunId,
  storeId: "store-1",
  documentId: "doc-1",
  status,
  targetChoice: "current_selection",
  targetLabel: "Selected passage",
  capturedAt: "2026-08-09T12:00:00Z",
  structuredHeadSha256: "a".repeat(64),
  projectionSha256: "b".repeat(64),
  execution: {
    providerId: "claude-code",
    modelId: "sonnet",
    providerLabel: "Claude Code",
    modelLabel: "Sonnet",
  },
  candidates: [],
  sourceCoverage: [],
  limitations: [],
  error: null,
  createdAt: "2026-08-09T12:00:00Z",
  finishedAt: status === "completed" ? "2026-08-09T12:00:05Z" : null,
});

const deferred = <T,>() => {
  let resolve: (value: T) => void = () => undefined;
  let reject: (cause: unknown) => void = () => undefined;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
};

const capabilities: TruthAnalysisCapabilities = {
  schema: "wb.cowork.truth-analysis-capabilities/v1",
  requiredCostControl: {
    enforcementClass: "hard_ceiling",
    scope: "worker_model_session",
    maximumUsdPerModelSession: 2,
  },
  researchCostControl: {
    enforcementClass: "unavailable",
    scope: "web_search_and_fetch",
    ceilingUsd: null,
    basis: "research_provider_cost_not_enforced",
  },
  providers: [],
};

const provider = (
  current: TruthAnalysisRun | null,
): TruthAnalysisProvider => ({
  loadCapabilities: vi.fn(async () => capabilities),
  loadCurrent: vi.fn(async () => current),
  loadRun: vi.fn(async (analysisRunId) => run(analysisRunId, "running")),
  start: vi.fn(),
  decideCandidate: vi.fn(),
  subscribe: vi.fn(() => () => undefined),
});

const flush = async (): Promise<void> => {
  await act(async () => {
    await Promise.resolve();
  });
};

afterEach(() => {
  vi.useRealTimers();
});

describe("useTruthAnalysis", () => {
  it("loads cost capabilities independently and recovers after a failed attestation", async () => {
    const source = provider(null);
    vi.mocked(source.loadCapabilities).mockRejectedValueOnce(
      new Error("Cost controls could not be verified."),
    );
    const hook = renderHook(() => useTruthAnalysis(source));
    await flush();

    expect(hook.result.current.status).toBe("ready");
    expect(hook.result.current.capabilitiesStatus).toBe("error");
    expect(hook.result.current.capabilities).toBeNull();
    expect(hook.result.current.capabilitiesError).toBe(
      "Cost controls could not be verified.",
    );

    vi.mocked(source.loadCapabilities).mockResolvedValue(capabilities);
    act(() => hook.result.current.reload());
    await flush();
    expect(hook.result.current.capabilitiesStatus).toBe("ready");
    expect(hook.result.current.capabilities).toEqual(capabilities);
  });

  it("keeps a slow poll single-flight and adopts its successful result", async () => {
    vi.useFakeTimers();
    const pending = run("run-a", "running");
    const completed = run("run-a", "completed");
    const slow = deferred<TruthAnalysisRun>();
    const source = provider(pending);
    vi.mocked(source.loadRun).mockReturnValue(slow.promise);
    const hook = renderHook(() => useTruthAnalysis(source, 1_000));
    await flush();

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(source.loadRun).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(source.loadRun).toHaveBeenCalledOnce();

    await act(async () => slow.resolve(completed));
    expect(hook.result.current.run).toEqual(completed);
    expect(hook.result.current.error).toBeNull();
  });

  it("does not let a late response from an old provider replace current state", async () => {
    vi.useFakeTimers();
    const oldPoll = deferred<TruthAnalysisRun>();
    const oldProvider = provider(run("run-old", "running"));
    vi.mocked(oldProvider.loadRun).mockReturnValue(oldPoll.promise);
    const next = run("run-new", "completed");
    const newProvider = provider(next);
    const hook = renderHook(
      ({ source }) => useTruthAnalysis(source, 1_000),
      { initialProps: { source: oldProvider } },
    );
    await flush();
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(oldProvider.loadRun).toHaveBeenCalledOnce();

    hook.rerender({ source: newProvider });
    await flush();
    expect(hook.result.current.run).toEqual(next);

    await act(async () => oldPoll.resolve(run("run-old", "completed")));
    expect(hook.result.current.run).toEqual(next);
  });

  it("does not keep polling an old run when the replacement provider cannot load", async () => {
    vi.useFakeTimers();
    const oldProvider = provider(run("run-old", "running"));
    const newProvider = provider(null);
    vi.mocked(newProvider.loadCurrent).mockRejectedValue(
      new Error("Replacement history unavailable."),
    );
    const hook = renderHook(
      ({ source }) => useTruthAnalysis(source, 1_000),
      { initialProps: { source: oldProvider } },
    );
    await flush();
    expect(hook.result.current.run?.analysisRunId).toBe("run-old");

    hook.rerender({ source: newProvider });
    await flush();
    expect(hook.result.current.run).toBeNull();
    expect(hook.result.current.status).toBe("error");
    expect(hook.result.current.error).toBe("Replacement history unavailable.");

    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(newProvider.loadRun).not.toHaveBeenCalled();
  });

  it("recovers after one transient polling failure", async () => {
    vi.useFakeTimers();
    const pending = run("run-a", "running");
    const completed = run("run-a", "completed");
    const source = provider(pending);
    vi.mocked(source.loadRun)
      .mockRejectedValueOnce(new Error("Temporary polling failure."))
      .mockResolvedValueOnce(completed);
    const hook = renderHook(() => useTruthAnalysis(source, 1_000));
    await flush();

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(hook.result.current.run).toEqual(pending);
    expect(hook.result.current.error).toBe("Temporary polling failure.");
    expect(source.loadRun).toHaveBeenCalledOnce();

    await act(async () => vi.advanceTimersByTimeAsync(999));
    expect(source.loadRun).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(source.loadRun).toHaveBeenCalledTimes(2);
    expect(hook.result.current.run).toEqual(completed);
    expect(hook.result.current.error).toBeNull();
  });
});
