import { describe, expect, it } from "vitest";

import type {
  ChatConversationSnapshot,
  RawChatConversationPayload,
} from "./contracts";
import {
  deriveAgentActivity,
  normalizeConversationPayload,
  toAgentLiveness,
  toAuthorRole,
} from "./mapping";

describe("toAuthorRole", () => {
  it("maps the backend agent role onto assistant and preserves user and system", () => {
    expect(toAuthorRole("agent")).toBe("assistant");
    expect(toAuthorRole("assistant")).toBe("assistant");
    expect(toAuthorRole("user")).toBe("user");
    expect(toAuthorRole("system")).toBe("system");
    expect(toAuthorRole(undefined)).toBe("assistant");
  });
});

describe("toAgentLiveness", () => {
  it("maps agent_alive true/false/null onto the liveness enum", () => {
    expect(toAgentLiveness(true)).toBe("alive");
    expect(toAgentLiveness(false)).toBe("stopped");
    expect(toAgentLiveness(null)).toBe("unknown");
    expect(toAgentLiveness(undefined)).toBe("unknown");
  });
});

describe("normalizeConversationPayload", () => {
  it("normalizes the raw conversation payload into canonical types", () => {
    const payload: RawChatConversationPayload = {
      conversation: {
        conversation_id: "c1",
        title: "Doc chat",
        status: "open",
        agent_alive: true,
      },
      messages: [
        {
          id: 1,
          role: "user",
          content: "Hi",
          created_at: "2026-07-17T12:00:00-04:00",
        },
        {
          id: 2,
          role: "agent",
          content: "Pick one",
          message_type: "question",
          status: "pending",
          response_type: "choice",
          choices: [
            { key: "a", label: "Option A" },
            { key: "b", label: "Option B" },
          ],
        },
      ],
    };

    const snapshot = normalizeConversationPayload(payload);

    expect(snapshot).toMatchObject({
      conversationId: "c1",
      title: "Doc chat",
      status: "open",
      agentLiveness: "alive",
    });
    expect(snapshot.messages).toHaveLength(2);
    expect(snapshot.messages[0]).toMatchObject({
      id: "1",
      author: "user",
      content: "Hi",
    });
    expect(snapshot.messages[1]).toMatchObject({
      id: "2",
      author: "assistant",
      pending: true,
      question: {
        responseType: "choice",
        choices: [
          { key: "a", label: "Option A" },
          { key: "b", label: "Option B" },
        ],
      },
    });
  });

  it("defaults missing fields and treats a closed status honestly", () => {
    const snapshot = normalizeConversationPayload({
      conversation: { conversation_id: "c2", status: "closed" },
    });
    expect(snapshot.status).toBe("closed");
    expect(snapshot.agentLiveness).toBe("unknown");
    expect(snapshot.messages).toEqual([]);
  });

  it("synthesizes a stable id when the backend omits one", () => {
    const snapshot = normalizeConversationPayload({
      conversation: { conversation_id: "c3" },
      messages: [{ role: "agent", content: "no id here" }],
    });
    expect(snapshot.messages[0]?.id).toBe("msg-0");
  });
});

const base = (
  overrides: Partial<ChatConversationSnapshot>,
): ChatConversationSnapshot => ({
  conversationId: "c",
  status: "open",
  agentLiveness: "alive",
  messages: [],
  ...overrides,
});

describe("deriveAgentActivity", () => {
  it("reports stopped when the driver process exited", () => {
    expect(
      deriveAgentActivity(base({ agentLiveness: "stopped" })),
    ).toBe("stopped");
  });

  it("is idle for a closed conversation regardless of liveness", () => {
    expect(
      deriveAgentActivity(base({ status: "closed", agentLiveness: "stopped" })),
    ).toBe("idle");
  });

  it("is idle while a question is pending", () => {
    expect(
      deriveAgentActivity(
        base({
          messages: [
            {
              id: "m1",
              author: "assistant",
              content: "?",
              pending: true,
              question: { responseType: "freeform" },
            },
          ],
        }),
      ),
    ).toBe("idle");
  });

  it("shows thinking while a live agent holds the last turn as text", () => {
    expect(
      deriveAgentActivity(
        base({
          messages: [{ id: "m1", author: "assistant", content: "Looking..." }],
        }),
      ),
    ).toBe("thinking");
  });

  it("shows thinking after the human replies even with no registered driver", () => {
    expect(
      deriveAgentActivity(
        base({
          agentLiveness: "unknown",
          messages: [{ id: "m1", author: "user", content: "and this?" }],
        }),
      ),
    ).toBe("thinking");
  });

  it("is idle when no driver is registered and the agent holds the turn", () => {
    expect(
      deriveAgentActivity(
        base({
          agentLiveness: "unknown",
          messages: [{ id: "m1", author: "assistant", content: "done" }],
        }),
      ),
    ).toBe("idle");
  });
});
