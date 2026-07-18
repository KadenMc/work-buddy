import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  ChatConversationProvider,
  ChatConversationSnapshot,
} from "./contracts";
import { InMemoryChatProvider } from "./InMemoryChatProvider";
import { useChatConversation } from "./useChatConversation";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const emptySnapshot = (conversationId: string): ChatConversationSnapshot => ({
  conversationId,
  status: "open",
  agentLiveness: "unknown",
  messages: [],
});

describe("useChatConversation", () => {
  it("loads the initial snapshot and reaches the ready state", async () => {
    const provider = new InMemoryChatProvider({
      conversationId: "c1",
      messages: [{ id: "m1", author: "assistant", content: "Hello" }],
    });
    const { result } = renderHook(() => useChatConversation(provider, "c1"));

    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.snapshot?.messages).toHaveLength(1);
  });

  it("appends a sent message and clears the sending flag", async () => {
    const provider = new InMemoryChatProvider({
      conversationId: "c1",
      autoReply: () => [{ id: "a1", author: "assistant", content: "Got it" }],
    });
    const { result } = renderHook(() => useChatConversation(provider, "c1"));
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.send("hi");
    });

    expect(result.current.sending).toBe(false);
    expect(result.current.snapshot?.messages.map((m) => m.content)).toEqual([
      "hi",
      "Got it",
    ]);
  });

  it("reloads on a provider invalidation", async () => {
    const provider = new InMemoryChatProvider({ conversationId: "c1" });
    const { result } = renderHook(() => useChatConversation(provider, "c1"));
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.snapshot?.messages).toHaveLength(0);

    act(() => {
      provider.pushMessage({ id: "a1", author: "assistant", content: "ping" });
    });

    await waitFor(() =>
      expect(result.current.snapshot?.messages).toHaveLength(1),
    );
  });

  it("records a send error and rethrows so a draft can be retained", async () => {
    const provider = new InMemoryChatProvider({
      conversationId: "c1",
      failSend: true,
    });
    const { result } = renderHook(() => useChatConversation(provider, "c1"));
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await expect(result.current.send("x")).rejects.toThrow(
        /could not be delivered/,
      );
    });

    expect(result.current.sendError).toMatch(/could not be delivered/);
    expect(result.current.sending).toBe(false);
  });

  it("recovers from a load error on retry", async () => {
    const snapshot: ChatConversationSnapshot = {
      conversationId: "c1",
      status: "open",
      agentLiveness: "unknown",
      messages: [],
    };
    let failLoad = true;
    const provider: ChatConversationProvider = {
      loadConversation: vi.fn(async () => {
        if (failLoad) throw new Error("network down");
        return snapshot;
      }),
      sendMessage: vi.fn(),
      subscribe: () => () => {},
    };

    const { result } = renderHook(() => useChatConversation(provider, "c1"));
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.error).toMatch(/network down/);

    failLoad = false;
    act(() => {
      result.current.retry();
    });

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.error).toBeNull();
  });

  it("drops a stale send result that resolves after a rebind to another conversation", async () => {
    const pendingSend = deferred<ChatConversationSnapshot>();
    const provider: ChatConversationProvider = {
      loadConversation: async (conversationId) => emptySnapshot(conversationId),
      sendMessage: () => pendingSend.promise,
      subscribe: () => () => {},
    };
    const { result, rerender } = renderHook(
      ({ conversationId }) => useChatConversation(provider, conversationId),
      { initialProps: { conversationId: "conv-a" } },
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));

    let sendPromise!: Promise<void>;
    act(() => {
      sendPromise = result.current.send("late reply");
    });

    rerender({ conversationId: "conv-b" });
    await waitFor(() =>
      expect(result.current.snapshot?.conversationId).toBe("conv-b"),
    );

    await act(async () => {
      pendingSend.resolve({
        ...emptySnapshot("conv-a"),
        messages: [{ id: "stale-1", author: "user", content: "late reply" }],
      });
      await sendPromise;
    });

    // The stale result is dropped, conv-b's transcript is untouched.
    expect(result.current.snapshot?.conversationId).toBe("conv-b");
    expect(result.current.snapshot?.messages).toHaveLength(0);
  });

  it("does not surface a stale send failure under the new binding", async () => {
    const pendingSend = deferred<ChatConversationSnapshot>();
    const provider: ChatConversationProvider = {
      loadConversation: async (conversationId) => emptySnapshot(conversationId),
      sendMessage: () => pendingSend.promise,
      subscribe: () => () => {},
    };
    const { result, rerender } = renderHook(
      ({ conversationId }) => useChatConversation(provider, conversationId),
      { initialProps: { conversationId: "conv-a" } },
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));

    let sendPromise!: Promise<void>;
    act(() => {
      sendPromise = result.current.send("doomed reply");
    });

    rerender({ conversationId: "conv-b" });
    await waitFor(() =>
      expect(result.current.snapshot?.conversationId).toBe("conv-b"),
    );

    await act(async () => {
      pendingSend.reject(new Error("network blip"));
      await expect(sendPromise).rejects.toThrow(/network blip/);
    });

    // The stale failure belongs to conv-a's binding and must not paint an
    // error over conv-b.
    expect(result.current.sendError).toBeNull();
    expect(result.current.snapshot?.conversationId).toBe("conv-b");
  });
});
