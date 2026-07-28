import { describe, expect, it, vi } from "vitest";

import {
  HttpCoworkDocumentConversationBindingClient,
  normalizeCoworkDocumentAgent,
} from "./documentConversationBinding";

const jsonResponse = (
  body: unknown,
  status = 200,
): Response =>
  ({
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    json: async () => body,
  }) as unknown as Response;

const RUNNING_AGENT = {
  status: "running",
  alive: true,
  started: false,
  error: null,
} as const;

describe("HttpCoworkDocumentConversationBindingClient", () => {
  it("loads the server-issued opaque id with a read-only GET", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        ok: true,
        conversation_id: "990a6d4897eb",
        agent: RUNNING_AGENT,
      }),
    );
    const client = new HttpCoworkDocumentConversationBindingClient(
      fetchImpl as unknown as typeof fetch,
    );

    const binding = await client.load("doc / 1", "store?one");

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/truth/doc/doc%20%2F%201/conversation?store_id=store%3Fone",
      {
        method: "GET",
        headers: { Accept: "application/json" },
      },
    );
    expect(binding).toEqual({
      conversationId: "990a6d4897eb",
      created: false,
      agent: RUNNING_AGENT,
      feedback: [],
    });
  });

  it("normalizes persisted feedback by its exact message id", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        ok: true,
        conversation_id: "opaque-feedback-chat",
        agent: RUNNING_AGENT,
        feedback: [
          {
            evidence_id: "ev-7",
            span_id: "span-7",
            conversation_id: "opaque-feedback-chat",
            message_id: "message-7",
            text: "Use a measurable claim.",
            anchor: {
              exact: "very effective",
              prefix: "This is ",
              suffix: " today.",
              node_id_hint: null,
            },
          },
        ],
      }),
    );
    const client = new HttpCoworkDocumentConversationBindingClient(
      fetchImpl as unknown as typeof fetch,
    );

    const binding = await client.load("doc-7", "store-7");

    expect(binding.feedback).toEqual([
      {
        documentId: "doc-7",
        storeId: "store-7",
        evidenceId: "ev-7",
        spanId: "span-7",
        conversationId: "opaque-feedback-chat",
        messageId: "message-7",
        text: "Use a measurable claim.",
        anchor: {
          exact: "very effective",
          prefix: "This is ",
          suffix: " today.",
          nodeIdHint: null,
        },
      },
    ]);
  });

  it("uses POST only for an explicit ensure and preserves the returned id", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        ok: true,
        conversation_id: "server-issued-a4e9",
        created: true,
        agent: { ...RUNNING_AGENT, started: true },
      }),
    );
    const client = new HttpCoworkDocumentConversationBindingClient(
      fetchImpl as unknown as typeof fetch,
    );

    const binding = await client.ensure("doc-1", "store-1");

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/truth/doc/doc-1/conversation?store_id=store-1",
      {
        method: "POST",
        headers: { Accept: "application/json" },
      },
    );
    expect(binding.conversationId).toBe("server-issued-a4e9");
    expect(binding.created).toBe(true);
    expect(binding.agent.started).toBe(true);
  });

  it("fails closed on a malformed success payload", async () => {
    const client = new HttpCoworkDocumentConversationBindingClient(
      vi.fn(async () =>
        jsonResponse({ conversation_id: "looks-real-but-is-untrusted" }),
      ) as unknown as typeof fetch,
    );

    await expect(client.load("doc-1", "store-1")).rejects.toThrow(
      "could not be loaded",
    );
  });

  it("rejects feedback that points at another conversation", async () => {
    const client = new HttpCoworkDocumentConversationBindingClient(
      vi.fn(async () =>
        jsonResponse({
          ok: true,
          conversation_id: "bound-chat",
          agent: RUNNING_AGENT,
          feedback: [
            {
              evidence_id: "ev-1",
              span_id: "span-1",
              conversation_id: "other-chat",
              message_id: "message-1",
              text: "Note",
              anchor: {
                exact: "quote",
                prefix: "",
                suffix: "",
                node_id_hint: null,
              },
            },
          ],
        }),
      ) as unknown as typeof fetch,
    );

    await expect(client.load("doc-1", "store-1")).rejects.toThrow(
      /mismatched feedback/i,
    );
  });

  it("parses the earlier flat agent fields defensively without inventing started", () => {
    expect(
      normalizeCoworkDocumentAgent({
        agent_status: "stopped",
        agent_error: "runner exited",
      }),
    ).toEqual({
      status: "stopped",
      alive: null,
      started: false,
      error: "runner exited",
    });
  });
});
