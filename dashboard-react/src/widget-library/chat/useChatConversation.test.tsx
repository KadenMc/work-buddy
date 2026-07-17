import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  ChatConversationProvider,
  ChatConversationSnapshot,
} from "./contracts";
import { InMemoryChatProvider } from "./InMemoryChatProvider";
import { useChatConversation } from "./useChatConversation";

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
});
