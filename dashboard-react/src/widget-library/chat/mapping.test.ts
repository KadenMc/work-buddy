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
          message_id: "m-1",
          role: "user",
          content: "Hi",
          created_at: "2026-07-17T12:00:00-04:00",
        },
        {
          message_id: "m-2",
          role: "agent",
          content: "Pick one",
          producer: {
            provider_id: "codex",
            model_id: "gpt-5.6",
            provider_label: "Codex",
            model_label: "GPT-5.6",
          },
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
      id: "m-1",
      author: "user",
      content: "Hi",
    });
    expect(snapshot.messages[1]).toMatchObject({
      id: "m-2",
      author: "assistant",
      pending: true,
      question: {
        responseType: "choice",
        choices: [
          { key: "a", label: "Option A" },
          { key: "b", label: "Option B" },
        ],
      },
      producer: {
        providerId: "codex",
        modelId: "gpt-5.6",
        providerLabel: "Codex",
        modelLabel: "GPT-5.6",
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

  it("preserves receipt-bound consumed-target and Co-think provenance", () => {
    const snapshot = normalizeConversationPayload({
      conversation: { conversation_id: "c-target" },
      messages: [
        {
          message_id: "reply-1",
          role: "agent",
          content: "I used the exact target.",
          context: {
            kind: "action_snapshot",
            action_snapshot_id: "action-1",
            store_id: "store-1",
            document_id: "doc-1",
            target_kind: "document",
            target_label: "Whole document",
            target_text_sha256: "a".repeat(64),
            projection_sha256: "b".repeat(64),
            captured_at: "2026-07-28T00:00:00Z",
            consumption: {
              receipt_id: "receipt-1",
              user_message_id: "user-1",
              fetched_at: "2026-07-28T00:00:01Z",
            },
            discussion: {
              kind: "cothink_item",
              item_id: "item-1",
              canonical_sha256: "c".repeat(64),
              content: "What if the choice is reversible?",
              rationale: "Challenge the framing.",
              non_evidential: true,
            },
          },
        },
      ],
    });

    expect(snapshot.messages[0]?.context).toMatchObject({
      actionSnapshotId: "action-1",
      consumption: {
        receiptId: "receipt-1",
        userMessageId: "user-1",
      },
      discussion: {
        kind: "cothink_item",
        itemId: "item-1",
        nonEvidential: true,
      },
    });
  });

  it("preserves a typed unavailable frozen-context receipt", () => {
    const snapshot = normalizeConversationPayload({
      conversation: { conversation_id: "c-unavailable" },
      messages: [
        {
          message_id: "reply-unavailable",
          role: "agent",
          content: "I could not open the exact frozen context.",
          context: {
            kind: "action_snapshot",
            action_snapshot_id: "action-unavailable",
            store_id: "store-1",
            document_id: "doc-1",
            target_kind: "text_quote",
            target_label: "Introduction",
            target_text_sha256: "a".repeat(64),
            projection_sha256: "b".repeat(64),
            captured_at: "2026-07-28T00:00:00Z",
            consumption: {
              receipt_id: "receipt-unavailable",
              user_message_id: "user-unavailable",
              fetched_at: "2026-07-28T00:00:01Z",
              fetch_outcome: "unavailable",
              unavailable_code: "action_snapshot_unavailable",
            },
          },
        },
      ],
    });

    expect(snapshot.messages[0]?.context?.consumption).toEqual({
      receiptId: "receipt-unavailable",
      userMessageId: "user-unavailable",
      fetchedAt: "2026-07-28T00:00:01Z",
      fetchOutcome: "unavailable",
      unavailableCode: "action_snapshot_unavailable",
    });
  });

  it("falls back to a bare id when only a fixture-shaped id is present", () => {
    const snapshot = normalizeConversationPayload({
      conversation: { conversation_id: "c3" },
      messages: [{ id: 7, role: "user", content: "fixture shape" }],
    });
    expect(snapshot.messages[0]?.id).toBe("7");
  });

  it("prefers message_id over a bare id when both are present", () => {
    const snapshot = normalizeConversationPayload({
      conversation: { conversation_id: "c3" },
      messages: [
        { message_id: "m-9", id: 9, role: "user", content: "both fields" },
      ],
    });
    expect(snapshot.messages[0]?.id).toBe("m-9");
  });

  it("synthesizes a positional id when no identity field is present", () => {
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
  it("keeps an ordinary empty conversation idle regardless of driver liveness", () => {
    expect(deriveAgentActivity(base({ agentLiveness: "alive" }))).toBe(
      "idle",
    );
    expect(deriveAgentActivity(base({ agentLiveness: "unknown" }))).toBe("idle");
    expect(deriveAgentActivity(base({ agentLiveness: "stopped" }))).toBe("idle");
  });

  it("does not report a failure when an idle driver exits before any turn", () => {
    expect(
      deriveAgentActivity(base({ agentLiveness: "stopped" })),
    ).toBe("idle");
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

  it("is idle after an assistant reply even while its long-lived driver is alive", () => {
    expect(
      deriveAgentActivity(
        base({
          messages: [{ id: "m1", author: "assistant", content: "Looking..." }],
        }),
      ),
    ).toBe("idle");
  });

  it("reports no response when the driver exits with the human holding the turn", () => {
    expect(
      deriveAgentActivity(
        base({
          agentLiveness: "stopped",
          messages: [{ id: "m1", author: "user", content: "hello?" }],
        }),
      ),
    ).toBe("stopped");
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
