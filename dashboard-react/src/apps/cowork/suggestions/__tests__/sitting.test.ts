import { describe, expect, it, vi } from "vitest";

import {
  CoworkSittingClient,
  HttpCoworkSittingTransport,
  InMemoryCoworkSittingTransport,
  buildMaterializePayload,
  validateSitting,
} from "../sitting";
import type { DecisionItem } from "../types";

const decision = (proposalId = "proposal-1", verb: DecisionItem["verb"] = "confirm"):
  DecisionItem => ({
    proposal_id: proposalId,
    verb,
    canonical_sha256: "c".repeat(64),
  });

describe("two-phase Co-work sitting transport", () => {
  it("validates required human-authored fields", () => {
    expect(() => validateSitting([{ ...decision(), verb: "edit_confirm" }])).toThrow(
      /amend_content/,
    );
    expect(() =>
      validateSitting([{ ...decision(), verb: "edit_confirm", amend_content: "" }]),
    ).not.toThrow();
    expect(() =>
      validateSitting([{ ...decision(), verb: "edit_confirm", amend_content: "   " }]),
    ).toThrow(/whitespace-only/);
    expect(() => validateSitting([{ ...decision(), verb: "redirect" }])).toThrow(
      /redirect_note/,
    );
    expect(() =>
      validateSitting([{ ...decision(), verb: "reject_as_preference" }]),
    ).toThrow(/preference_text/);
  });

  it("hashes canonical rendered Markdown", async () => {
    const payload = await buildMaterializePayload("# exact\n");
    expect(payload.rendered_markdown).toBe("# exact\n");
    expect(payload.post_apply_content_sha256).toMatch(/^[a-f0-9]{64}$/u);
  });

  it("prepares idempotently, partially admits, and commits only admitted items", async () => {
    const transport = new InMemoryCoworkSittingTransport(["stale"]);
    const client = new CoworkSittingClient(transport);
    const request = {
      documentId: "doc",
      storeId: "store",
      body: {
        items: [decision("accepted"), decision("stale")],
        expected_file_sha256: "f".repeat(64),
        expected_ydoc_head_sha256: "h".repeat(64),
        idempotency_key: "same-key",
      },
    } as const;

    const first = await client.prepare(request);
    const retry = await client.prepare(request);
    expect(retry.intent_id).toBe(first.intent_id);
    expect(first.admitted_items.map((item) => item.proposal_id)).toEqual(["accepted"]);
    expect(first.failed_items.map((item) => item.proposal_id)).toEqual(["stale"]);

    const snapshot = new Uint8Array([1, 2, 3]);
    const result = await client.commit({
      documentId: "doc",
      storeId: "store",
      intentId: first.intent_id,
      documentCommit: {
        snapshot,
        snapshot_sha256: "s".repeat(64),
        rendered_markdown: "accepted\n",
        rendered_sha256: "m".repeat(64),
      },
    });
    expect(result.partial).toBe(true);
    expect(result.results.map((item) => item.result)).toEqual([
      "applied",
      "rejected_stale_view",
    ]);
    expect((await client.prepare(request)).result).toEqual(result);
  });

  it("uses prepare JSON and multipart commit on the canonical routes", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: true,
            intent_id: "intent-1",
            state: "prepared",
            expires_at: "later",
            expected_file_sha256: "f".repeat(64),
            expected_ydoc_head_sha256: "h".repeat(64),
            expected_snapshot_sha256: "s".repeat(64),
            admitted_items: [decision()],
            failed_items: [],
            requires_document_commit: true,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: true,
            intent_id: "intent-1",
            partial: false,
            results: [],
            materialize: null,
            structured_head_sha256: "s".repeat(64),
            snapshot_sha256: "s".repeat(64),
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    const transport = new HttpCoworkSittingTransport(fetchImpl);
    const prepared = await transport.prepare({
      documentId: "doc one",
      storeId: "store/one",
      body: {
        items: [decision()],
        expected_file_sha256: "f".repeat(64),
        expected_ydoc_head_sha256: "h".repeat(64),
        idempotency_key: "key",
      },
    });
    await transport.commit({
      documentId: "doc one",
      storeId: "store/one",
      intentId: prepared.intent_id,
      documentCommit: {
        snapshot: new Uint8Array([1]),
        snapshot_sha256: "s".repeat(64),
        rendered_markdown: "text",
        rendered_sha256: "m".repeat(64),
      },
    });

    expect(fetchImpl.mock.calls[0]?.[0]).toBe(
      "/api/truth/doc/doc%20one/sitting/prepare?store_id=store%2Fone",
    );
    expect(JSON.parse(String((fetchImpl.mock.calls[0]?.[1] as RequestInit).body))).toMatchObject({
      idempotency_key: "key",
      expected_ydoc_head_sha256: "h".repeat(64),
    });
    expect(fetchImpl.mock.calls[1]?.[0]).toBe(
      "/api/truth/doc/doc%20one/sitting/intent-1/commit?store_id=store%2Fone",
    );
    const commitForm = (fetchImpl.mock.calls[1]?.[1] as RequestInit).body as FormData;
    expect(commitForm).toBeInstanceOf(FormData);
    expect(JSON.parse(String(commitForm.get("metadata")))).toEqual({
      snapshot_sha256: "s".repeat(64),
      rendered_sha256: "m".repeat(64),
    });
  });
});
