import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ReviewRailData } from "./contracts";
import type {
  ReviewInvalidationListener,
  ReviewRailProvider,
} from "./provider";
import { useReviewData } from "./useReviewData";

const data = (title: string): ReviewRailData => ({
  documentId: "doc-1",
  title,
  drift: {
    state: "clean",
    openProposalCount: 0,
    openFlagCount: 0,
    lastMaterializedSha256: null,
    currentFileSha256: null,
  },
  verifyCapability: {
    enabled: true,
    contractVersion: 1,
    canRun: true,
    canConfigure: true,
    canCothink: true,
    disabledReason: null,
  },
  verificationConfiguration: {
    schema: "work-buddy.cowork-verify-configuration/v1",
    documentId: "doc",
    executionPlan: null,
    coordination: null,
    criteria: [],
  },
  evaluationRuns: [],
  evaluationResults: [],
  verificationRecheckIntents: [],
  cothinkItems: [],
  cothinkOutcomes: [],
  proposals: [],
  expressions: [],
  provenanceSpans: [],
  claims: [],
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
};

describe("useReviewData", () => {
  it("ignores an older invalidation load that resolves after a newer one", async () => {
    const initial = deferred<ReviewRailData>();
    const older = deferred<ReviewRailData>();
    const newer = deferred<ReviewRailData>();
    const listeners = new Set<ReviewInvalidationListener>();
    const provider: ReviewRailProvider = {
      load: vi
        .fn<() => Promise<ReviewRailData>>()
        .mockReturnValueOnce(initial.promise)
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(newer.promise),
      subscribe: (listener) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      submitSitting: vi.fn(),
    };
    const hook = renderHook(() => useReviewData(provider));

    initial.resolve(data("initial"));
    await waitFor(() => expect(hook.result.current.data?.title).toBe("initial"));

    act(() => {
      for (const listener of listeners) listener();
      for (const listener of listeners) listener();
    });
    newer.resolve(data("newer"));
    await waitFor(() => expect(hook.result.current.data?.title).toBe("newer"));

    older.resolve(data("older"));
    await act(async () => {
      await older.promise;
      await Promise.resolve();
    });
    expect(hook.result.current.data?.title).toBe("newer");
  });
});
