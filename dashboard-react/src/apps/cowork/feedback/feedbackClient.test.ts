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

describe("HttpCoworkFeedbackTransport", () => {
  it("POSTs the R9 route with the store_id query and the span-plus-text body", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({
        ok: true,
        evidence_id: "ev-1",
        span_id: "sp-1",
        conversation_id: "c-1",
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
    expect(response.conversation_id).toBe("c-1");

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

  it("throws on a non-2xx response so the caller can preserve the typed text", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: "nope" }, { status: 403 }),
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
    ).rejects.toThrow(/403/);
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
      conversation_id: "cowork-doc-d1",
    });
    expect(transport.lastRequest?.text).toBe("note");
    expect(transport.lastRequest?.span.exact).toBe("x");
  });
});
