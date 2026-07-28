import { describe, expect, it, vi } from "vitest";

import {
  HttpCoworkFeedbackTransport,
  InMemoryCoworkFeedbackTransport,
} from "./feedbackClient";

const jsonResponse = (
  body: unknown,
  init?: { ok?: boolean; status?: number },
): Response =>
  ({
    ok: init?.ok ?? (init?.status ?? 200) < 400,
    status: init?.status ?? 200,
    json: async () => body,
  }) as unknown as Response;

const rawExecution = {
  selection: {
    provider_id: "codex",
    model_id: "gpt-5.6-sol",
    provider_label: "Codex",
    model_label: "GPT-5.6 Sol",
    revision: "execution:feedback",
  },
  providers: [
    {
      id: "codex",
      label: "Codex",
      available: true,
      models: [
        {
          id: "gpt-5.6-sol",
          label: "GPT-5.6 Sol",
          available: true,
        },
      ],
    },
  ],
} as const;

describe("HttpCoworkFeedbackTransport", () => {
  it("POSTs the R9 route with the store_id query and the span-plus-text body", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        ok: true,
        evidence_id: "ev-1",
        span_id: "sp-1",
        message_id: "message-1",
        conversation_id: "c-1",
        agent: {
          status: "running",
          alive: true,
          started: true,
          error: null,
        },
        execution: rawExecution,
      }),
    );
    const transport = new HttpCoworkFeedbackTransport(
      fetchImpl as unknown as typeof fetch,
    );

    const response = await transport.submit({
      documentId: "doc 1",
      storeId: "store/x",
      span: {
        exact: "precise",
        prefix: "make this ",
        suffix: " please",
        node_id_hint: null,
      },
      text: "tighten this",
    });

    expect(response.evidence_id).toBe("ev-1");
    expect(response.span_id).toBe("sp-1");
    expect(response.message_id).toBe("message-1");
    expect(response.conversation_id).toBe("c-1");
    expect(response.agent.status).toBe("running");
    expect(response.execution.selection).toMatchObject({
      providerId: "codex",
      modelId: "gpt-5.6-sol",
      revision: "execution:feedback",
    });

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    // documentId and storeId are URL-encoded into the frozen R9 path and query.
    expect(url).toBe("/api/truth/doc/doc%201/feedback?store_id=store%2Fx");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
    expect(JSON.parse(init.body as string)).toEqual({
      span: {
        exact: "precise",
        prefix: "make this ",
        suffix: " please",
        node_id_hint: null,
      },
      text: "tighten this",
    });
  });

  it("surfaces a structured human error for a rejected 4xx request", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        {
          error: {
            code: "policy_forbidden",
            message: "Feedback is disabled for this document.",
            retryable: false,
          },
        },
        { status: 403 },
      ),
    );
    const transport = new HttpCoworkFeedbackTransport(
      fetchImpl as unknown as typeof fetch,
    );

    await expect(
      transport.submit({
        documentId: "d",
        storeId: "s",
        span: { exact: "x", prefix: "", suffix: "", node_id_hint: null },
        text: "note",
      }),
    ).rejects.toThrow("Feedback is disabled for this document.");
  });

  it("uses reconciliation copy for a 5xx after a potentially durable write", async () => {
    const transport = new HttpCoworkFeedbackTransport(
      vi.fn(async () =>
        jsonResponse(
          { error: { message: "internal failure" } },
          { status: 503 },
        ),
      ) as unknown as typeof fetch,
    );

    await expect(
      transport.submit({
        documentId: "d",
        storeId: "s",
        span: { exact: "x", prefix: "", suffix: "", node_id_hint: null },
        text: "note",
      }),
    ).rejects.toThrow(/may have been saved.*reload before trying again/i);
  });

  it("uses reconciliation copy when the response is lost", async () => {
    const transport = new HttpCoworkFeedbackTransport(
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }) as unknown as typeof fetch,
    );

    await expect(
      transport.submit({
        documentId: "d",
        storeId: "s",
        span: { exact: "x", prefix: "", suffix: "", node_id_hint: null },
        text: "note",
      }),
    ).rejects.toThrow(/may have been saved.*reload before trying again/i);
  });

  it("reports a reconciliation error for a malformed successful response", async () => {
    const transport = new HttpCoworkFeedbackTransport(
      vi.fn(async () =>
        jsonResponse({
          ok: true,
          evidence_id: "ev-1",
          span_id: "span-1",
          conversation_id: "conversation-1",
          // message_id is required to link the durable turn unambiguously.
          agent: {
            status: "running",
            alive: true,
            started: true,
            error: null,
          },
        }),
      ) as unknown as typeof fetch,
    );

    await expect(
      transport.submit({
        documentId: "doc-1",
        storeId: "store-1",
        span: { exact: "quote", prefix: "", suffix: "", node_id_hint: null },
        text: "Keep this.",
      }),
    ).rejects.toThrow(/may have been saved.*reload before trying again/i);
  });
});

describe("InMemoryCoworkFeedbackTransport", () => {
  it("records the last request and returns a deterministic capture", async () => {
    const transport = new InMemoryCoworkFeedbackTransport();
    const response = await transport.submit({
      documentId: "d1",
      storeId: "s",
      span: { exact: "x", prefix: "", suffix: "", node_id_hint: null },
      text: "note",
    });
    expect(response).toEqual({
      ok: true,
      evidence_id: "ev-d1",
      span_id: "span-d1",
      message_id: "feedback-message-d1",
      conversation_id: "server-feedback-conversation-d1",
      agent: {
        status: "running",
        alive: true,
        started: true,
        error: null,
      },
      execution: {
        selection: {
          providerId: "claude-code",
          modelId: "sonnet",
          providerLabel: "Claude Code",
          modelLabel: "Sonnet",
          revision: "execution-d1",
        },
        providers: [
          {
            id: "claude-code",
            label: "Claude Code",
            available: true,
            models: [
              {
                id: "sonnet",
                label: "Sonnet",
                available: true,
              },
            ],
          },
        ],
      },
    });
    expect(transport.lastRequest?.text).toBe("note");
    expect(transport.lastRequest?.span.exact).toBe("x");
  });
});
