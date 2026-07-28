import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  ChatExecutionProfileProvider,
  ChatExecutionSnapshot,
} from "./contracts";
import {
  ChatExecutionSelectionError,
  useChatExecutionProfile,
} from "./useChatExecutionProfile";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const snapshot = (
  providerId = "claude-code",
  modelId = "sonnet",
  revision = "execution:1",
): ChatExecutionSnapshot => ({
  selection: {
    providerId,
    modelId,
    providerLabel: providerId === "codex" ? "Codex" : "Claude Code",
    modelLabel: modelId === "gpt-5.6" ? "GPT-5.6" : "Sonnet",
    revision,
  },
  providers: [
    {
      id: "claude-code",
      label: "Claude Code",
      available: true,
      models: [{ id: "sonnet", label: "Sonnet", available: true }],
    },
    {
      id: "codex",
      label: "Codex",
      available: true,
      models: [{ id: "gpt-5.6", label: "GPT-5.6", available: true }],
    },
  ],
});

const provider = (
  load: ChatExecutionProfileProvider["load"],
  select: ChatExecutionProfileProvider["select"],
): ChatExecutionProfileProvider => ({
  load,
  select,
  subscribe: () => () => {},
});

describe("useChatExecutionProfile", () => {
  it("loads and commits an atomic selection with the observed revision", async () => {
    const initial = snapshot();
    const changed = snapshot("codex", "gpt-5.6", "execution:2");
    const select = vi.fn(async () => changed);
    const executionProvider = provider(
      vi.fn(async () => initial),
      select,
    );
    const { result } = renderHook(() =>
      useChatExecutionProfile(executionProvider, "document:1"),
    );
    await waitFor(() => expect(result.current?.status).toBe("ready"));

    await act(async () => {
      await result.current?.select("codex", "gpt-5.6");
    });

    expect(select).toHaveBeenCalledWith("document:1", {
      providerId: "codex",
      modelId: "gpt-5.6",
      expectedRevision: "execution:1",
    });
    expect(result.current?.snapshot).toBe(changed);
    expect(result.current?.announcement).toBe(
      "Now using Codex · GPT-5.6.",
    );
  });

  it("keeps a selection authoritative over a subscription reload issued while it is pending", async () => {
    const initial = snapshot();
    const stale = snapshot("claude-code", "sonnet", "execution:stale");
    const changed = snapshot("codex", "gpt-5.6", "execution:2");
    const staleLoad = deferred<ChatExecutionSnapshot>();
    const pendingSelection = deferred<ChatExecutionSnapshot>();
    let invalidate = () => {};
    let loadCount = 0;
    const load = vi.fn(() => {
      loadCount += 1;
      return loadCount === 1 ? Promise.resolve(initial) : staleLoad.promise;
    });
    const executionProvider: ChatExecutionProfileProvider = {
      load,
      select: vi.fn(() => pendingSelection.promise),
      subscribe: (_targetId, onInvalidate) => {
        invalidate = onInvalidate;
        return () => {};
      },
    };
    const { result } = renderHook(() =>
      useChatExecutionProfile(executionProvider, "document:1"),
    );
    await waitFor(() => expect(result.current?.status).toBe("ready"));

    let selection!: Promise<void>;
    act(() => {
      selection = result.current!.select("codex", "gpt-5.6");
      invalidate();
    });
    expect(load).toHaveBeenCalledTimes(2);

    await act(async () => {
      staleLoad.resolve(stale);
      await staleLoad.promise;
    });
    expect(result.current?.snapshot).toBe(initial);

    await act(async () => {
      pendingSelection.resolve(changed);
      await selection;
    });
    expect(result.current?.snapshot).toBe(changed);
    expect(result.current?.announcement).toBe(
      "Now using Codex · GPT-5.6.",
    );
  });

  it("adopts the authoritative snapshot returned with a conflict", async () => {
    const authoritative = snapshot("codex", "gpt-5.6", "execution:9");
    const executionProvider = provider(
      vi.fn(async () => snapshot()),
      vi.fn(async () => {
        throw new ChatExecutionSelectionError(
          "The model selection changed elsewhere.",
          authoritative,
        );
      }),
    );
    const { result } = renderHook(() =>
      useChatExecutionProfile(executionProvider, "document:1"),
    );
    await waitFor(() => expect(result.current?.status).toBe("ready"));

    await act(async () => {
      await expect(
        result.current?.select("codex", "gpt-5.6"),
      ).rejects.toThrow("changed elsewhere");
    });

    expect(result.current?.snapshot).toBe(authoritative);
    expect(result.current?.error).toBe(
      "The model selection changed elsewhere.",
    );
    expect(result.current?.selecting).toBe(false);
  });

  it("drops a load that resolves after the target is rebound", async () => {
    const oldLoad = deferred<ChatExecutionSnapshot>();
    const load = vi.fn((targetId: string) =>
      targetId === "document:old"
        ? oldLoad.promise
        : Promise.resolve(snapshot("codex", "gpt-5.6", "execution:new")),
    );
    const executionProvider = provider(load, vi.fn());
    const { result, rerender } = renderHook(
      ({ targetId }) =>
        useChatExecutionProfile(executionProvider, targetId),
      { initialProps: { targetId: "document:old" } },
    );

    rerender({ targetId: "document:new" });
    await waitFor(() =>
      expect(result.current?.snapshot?.selection.revision).toBe(
        "execution:new",
      ),
    );

    await act(async () => {
      oldLoad.resolve(snapshot("claude-code", "sonnet", "execution:old"));
      await oldLoad.promise;
    });
    expect(result.current?.snapshot?.selection.revision).toBe("execution:new");
  });

  it("allows only one atomic selection request at a time", async () => {
    const pending = deferred<ChatExecutionSnapshot>();
    const executionProvider = provider(
      vi.fn(async () => snapshot()),
      vi.fn(() => pending.promise),
    );
    const { result } = renderHook(() =>
      useChatExecutionProfile(executionProvider, "document:1"),
    );
    await waitFor(() => expect(result.current?.status).toBe("ready"));

    let first!: Promise<void>;
    let duplicate!: Promise<void>;
    act(() => {
      first = result.current!.select("codex", "gpt-5.6");
      duplicate = result.current!.select("codex", "gpt-5.6");
    });
    await expect(duplicate).rejects.toThrow("not ready");
    expect(result.current?.selecting).toBe(true);

    await act(async () => {
      pending.resolve(snapshot("codex", "gpt-5.6", "execution:2"));
      await first;
    });
    expect(result.current?.selecting).toBe(false);
  });

  it("asks the provider to refresh before retrying discovery", async () => {
    let current = snapshot();
    const refresh = vi.fn(() => {
      current = snapshot("codex", "gpt-5.6", "execution:refreshed");
    });
    const executionProvider: ChatExecutionProfileProvider = {
      load: vi.fn(async () => current),
      select: vi.fn(),
      refresh,
      subscribe: () => () => {},
    };
    const { result } = renderHook(() =>
      useChatExecutionProfile(executionProvider, "document:1"),
    );
    await waitFor(() => expect(result.current?.status).toBe("ready"));

    act(() => result.current?.retry());

    await waitFor(() =>
      expect(result.current?.snapshot?.selection.revision).toBe(
        "execution:refreshed",
      ),
    );
    expect(refresh).toHaveBeenCalledWith("document:1");
  });
});
