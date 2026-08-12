import { describe, expect, it, vi } from "vitest";

vi.mock("../../../security/humanAuthority", () => ({
  coworkHumanAuthorityHeaders: vi.fn(async () => ({})),
}));

import {
  CoworkDocumentConversationBindingError,
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

  it("prepares a binding without starting a model", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        ok: true,
        conversation_id: "server-issued-a4e9",
        created: true,
        agent: {
          status: "not_started",
          alive: null,
          started: false,
          error: null,
        },
      }),
    );
    const client = new HttpCoworkDocumentConversationBindingClient(
      fetchImpl as unknown as typeof fetch,
    );

    const binding = await client.ensure("doc-1", "store-1");

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/truth/doc/doc-1/conversation/bind?store_id=store-1",
      {
        method: "POST",
        headers: { Accept: "application/json" },
      },
    );
    expect(binding.conversationId).toBe("server-issued-a4e9");
    expect(binding.created).toBe(true);
    expect(binding.agent).toMatchObject({
      status: "not_started",
      started: false,
    });
  });

  it("does not send model-selection or lifecycle instructions while preparing", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        ok: true,
        conversation_id: "server-issued-codex",
        created: true,
        agent: {
          status: "not_started",
          alive: null,
          started: false,
          error: null,
        },
      }),
    );
    const client = new HttpCoworkDocumentConversationBindingClient(
      fetchImpl as unknown as typeof fetch,
    );

    await client.ensure("doc-1", "store-1");

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/truth/doc/doc-1/conversation/bind?store_id=store-1",
      {
        method: "POST",
        headers: { Accept: "application/json" },
      },
    );
  });

  it("preserves authoritative execution from a preparation failure", async () => {
    const client = new HttpCoworkDocumentConversationBindingClient(
      vi.fn(async () =>
        jsonResponse(
          {
            ok: false,
            error: {
              code: "execution_selection_changed",
              message: "The model choice changed elsewhere.",
            },
            execution: {
              selection: {
                provider_id: "codex",
                model_id: "gpt-5.6",
                provider_label: "Codex",
                model_label: "GPT-5.6",
                revision: "revision:new",
              },
              providers: [
                {
                  id: "codex",
                  label: "Codex",
                  available: true,
                  models: [
                    {
                      id: "gpt-5.6",
                      label: "GPT-5.6",
                      available: true,
                    },
                  ],
                },
              ],
            },
          },
          409,
        ),
      ) as unknown as typeof fetch,
    );

    const caught = await client
      .ensure("doc-1", "store-1")
      .catch((error: unknown) => error);

    expect(caught).toBeInstanceOf(
      CoworkDocumentConversationBindingError,
    );
    expect(
      (caught as CoworkDocumentConversationBindingError)
        .authoritativeExecution?.selection,
    ).toMatchObject({
      providerId: "codex",
      modelId: "gpt-5.6",
      revision: "revision:new",
    });
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
