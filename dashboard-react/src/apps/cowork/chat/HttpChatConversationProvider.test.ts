import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpChatConversationProvider } from "./HttpChatConversationProvider";

/** Build a minimal Response-like object for the injected fetch. */
function jsonResponse(
  body: unknown,
  init?: { ok?: boolean; status?: number; statusText?: string },
): Response {
  const status = init?.status ?? 200;
  return {
    ok: init?.ok ?? (status >= 200 && status < 300),
    status,
    statusText: init?.statusText ?? "",
    json: async () => body,
  } as unknown as Response;
}

interface RawMessage {
  readonly message_id: string;
  readonly role: string;
  readonly content: string;
  readonly created_at?: string;
  readonly message_type?: string;
  readonly status?: string;
}

function conversation(
  messages: readonly RawMessage[],
  init?: { status?: string; agent_alive?: boolean | null },
) {
  return {
    conversation: {
      conversation_id: "c1",
      title: "Document conversation",
      status: init?.status ?? "open",
      agent_alive: init?.agent_alive ?? true,
    },
    messages,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("HttpChatConversationProvider", () => {
  it("loads a conversation and normalizes the house payload", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        conversation(
          [{ message_id: "m1", role: "agent", content: "Hello" }],
          { agent_alive: true },
        ),
      ),
    );
    const provider = new HttpChatConversationProvider({
      conversationId: "c1",
      fetchImpl,
      pollIntervalMs: 0,
    });

    const snapshot = await provider.loadConversation("c1");

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/conversations/c1",
      expect.objectContaining({ method: "GET" }),
    );
    expect(snapshot.conversationId).toBe("c1");
    expect(snapshot.status).toBe("open");
    expect(snapshot.agentLiveness).toBe("alive");
    expect(snapshot.messages).toHaveLength(1);
    // The house "agent" role maps onto the canonical "assistant" author.
    expect(snapshot.messages[0]).toMatchObject({
      id: "m1",
      author: "assistant",
      content: "Hello",
    });
  });

  it("throws the server error text when the load fails", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: "Conversation not found" }, { status: 404 }),
    );
    const provider = new HttpChatConversationProvider({
      conversationId: "c1",
      fetchImpl,
      pollIntervalMs: 0,
    });

    await expect(provider.loadConversation("c1")).rejects.toThrow(
      "Conversation not found",
    );
  });

  it("throws when the payload is not a conversation", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ nope: true }));
    const provider = new HttpChatConversationProvider({
      conversationId: "c1",
      fetchImpl,
      pollIntervalMs: 0,
    });

    await expect(provider.loadConversation("c1")).rejects.toThrow();
  });

  it("posts a human turn to respond then reloads the next snapshot", async () => {
    const userMessage: RawMessage = {
      message_id: "u1",
      role: "user",
      content: "please tighten this",
    };
    const agentReply: RawMessage = {
      message_id: "a1",
      role: "agent",
      content: "On it.",
    };
    let posted = false;
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        posted = true;
        return jsonResponse({ sent: true, message_id: "u1" });
      }
      return jsonResponse(
        conversation(posted ? [userMessage, agentReply] : []),
      );
    });
    const provider = new HttpChatConversationProvider({
      conversationId: "c1",
      fetchImpl,
      pollIntervalMs: 0,
    });

    const snapshot = await provider.sendMessage("c1", {
      value: "please tighten this",
    });

    const postCall = fetchImpl.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === "POST",
    );
    expect(postCall?.[0]).toBe("/api/conversations/c1/respond");
    expect(
      JSON.parse((postCall?.[1] as RequestInit).body as string),
    ).toEqual({ value: "please tighten this" });
    expect(snapshot.messages.map((message) => message.content)).toEqual([
      "please tighten this",
      "On it.",
    ]);
  });

  it("throws when respond returns a non-ok status", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse(
          { error: "Conversation not found or closed" },
          { status: 404 },
        );
      }
      return jsonResponse(conversation([]));
    });
    const provider = new HttpChatConversationProvider({
      conversationId: "c1",
      fetchImpl,
      pollIntervalMs: 0,
    });

    await expect(
      provider.sendMessage("c1", { value: "hi" }),
    ).rejects.toThrow("Conversation not found or closed");
  });

  it("throws when respond returns 200 with an error body", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse({ error: "respond blew up" });
      }
      return jsonResponse(conversation([]));
    });
    const provider = new HttpChatConversationProvider({
      conversationId: "c1",
      fetchImpl,
      pollIntervalMs: 0,
    });

    await expect(
      provider.sendMessage("c1", { value: "hi" }),
    ).rejects.toThrow("respond blew up");
  });

  it("polls the invalidation listener on the conversation cadence", () => {
    vi.useFakeTimers();
    const provider = new HttpChatConversationProvider({
      conversationId: "c1",
      fetchImpl: vi.fn(),
      pollIntervalMs: 3000,
    });
    const listener = vi.fn();

    const unsubscribe = provider.subscribe("c1", listener);
    vi.advanceTimersByTime(3000);
    expect(listener).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(3000);
    expect(listener).toHaveBeenCalledTimes(2);

    unsubscribe();
    vi.advanceTimersByTime(9000);
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("keeps polling while any subscriber remains", () => {
    vi.useFakeTimers();
    const provider = new HttpChatConversationProvider({
      conversationId: "c1",
      fetchImpl: vi.fn(),
      pollIntervalMs: 1000,
    });
    const first = vi.fn();
    const second = vi.fn();

    const unsubFirst = provider.subscribe("c1", first);
    const unsubSecond = provider.subscribe("c1", second);
    vi.advanceTimersByTime(1000);
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);

    unsubFirst();
    vi.advanceTimersByTime(1000);
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(2);

    unsubSecond();
  });

  it("refuses a call bound to a different conversation", async () => {
    const provider = new HttpChatConversationProvider({
      conversationId: "c1",
      fetchImpl: vi.fn(),
      pollIntervalMs: 0,
    });

    await expect(provider.loadConversation("other")).rejects.toThrow(
      "bound to c1",
    );
    expect(() => provider.subscribe("other", vi.fn())).toThrow("bound to c1");
  });
});
