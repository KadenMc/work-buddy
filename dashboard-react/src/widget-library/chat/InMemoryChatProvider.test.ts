import { describe, expect, it, vi } from "vitest";

import { InMemoryChatProvider } from "./InMemoryChatProvider";

describe("InMemoryChatProvider", () => {
  it("loads the seeded snapshot", async () => {
    const provider = new InMemoryChatProvider({
      conversationId: "c1",
      title: "Seed",
      messages: [{ id: "m1", author: "assistant", content: "Hello" }],
    });
    const snapshot = await provider.loadConversation("c1");
    expect(snapshot.conversationId).toBe("c1");
    expect(snapshot.messages).toHaveLength(1);
  });

  it("rejects a load for an unknown conversation", async () => {
    const provider = new InMemoryChatProvider({ conversationId: "c1" });
    await expect(provider.loadConversation("other")).rejects.toThrow(
      /Unknown conversation/,
    );
  });

  it("appends the human turn and any scripted reply, notifying subscribers", async () => {
    const provider = new InMemoryChatProvider({
      conversationId: "c1",
      autoReply: () => [{ id: "a1", author: "assistant", content: "Got it" }],
    });
    const listener = vi.fn();
    provider.subscribe("c1", listener);

    const snapshot = await provider.sendMessage("c1", { value: "hi there" });

    expect(snapshot.messages.map((message) => message.content)).toEqual([
      "hi there",
      "Got it",
    ]);
    expect(snapshot.messages[0]?.author).toBe("user");
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("rejects a send when failSend is set and recovers when cleared", async () => {
    const provider = new InMemoryChatProvider({
      conversationId: "c1",
      failSend: true,
    });
    await expect(provider.sendMessage("c1", { value: "x" })).rejects.toThrow(
      /could not be delivered/,
    );
    provider.setFailSend(false);
    const snapshot = await provider.sendMessage("c1", { value: "x" });
    expect(snapshot.messages).toHaveLength(1);
  });

  it("stops delivering to a listener after unsubscribe", () => {
    const provider = new InMemoryChatProvider({ conversationId: "c1" });
    const listener = vi.fn();
    const unsubscribe = provider.subscribe("c1", listener);

    provider.pushMessage({ id: "a1", author: "assistant", content: "one" });
    expect(listener).toHaveBeenCalledTimes(1);

    unsubscribe();
    provider.pushMessage({ id: "a2", author: "assistant", content: "two" });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("notifies on liveness and status changes", () => {
    const provider = new InMemoryChatProvider({ conversationId: "c1" });
    const listener = vi.fn();
    provider.subscribe("c1", listener);

    provider.setAgentLiveness("stopped");
    provider.setStatus("closed");

    expect(listener).toHaveBeenCalledTimes(2);
  });
});
